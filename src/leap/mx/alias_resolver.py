#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
alias_resolver.py
=================
Classes for resolving postfix aliases.

:authors: Isis Agora Lovecruft
:version: 0.0.1-beta
:license: see included LICENSE file
:copyright: (c) 2013 Isis Agora Lovecruft

TODO:

    o Look into using twisted.protocols.postfix.policies classes for
      controlling concurrent connections and throttling resource consumption.
'''

import os
import uuid

try:
    from twisted.internet  import address, defer, reactor
    from twisted.mail      import maildir, alias
    from twisted.protocols import postfix
except ImportError:
    print "This software requires Twisted. Please see the README file"
    print "for instructions on getting required dependencies."

from leap.mx import couchdb
from leap.mx.util import net, log, config, exceptions


def createUUID(alias):
    """Creates Universal Unique ID by taking the SHA1 HASH of an email alias:

    >>> uuid.uuid5(uuid.NAMESPACE_URL, "isis@leap.se")
    UUID('7194878e-4aea-563f-85a4-4f58519f3c4f')

    TODO: Is there a commonly accepted way to check that an email address
          is valid?

    :param str alias: An email address alias.
    :returns: A :class:`uuid.UUID` containing attributes specifying the UUID.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, str(alias))


class DatabaseNotConnected(Exception):
    """Raised when not currently connected to a database."""

class StatusCodes(object):
    """The Postfix manual states:

        The request completion status is one of OK, RETRY, NOKEY (lookup failed
        because the key was not found), BAD (malformed request) or DENY (the
        table is not approved for proxy read or update access).

    In brief, Postfix will send ``get SPACE key NEWLINE``, or
    ``put SPACE key NEWLINE`` where ``key`` is an alias or email address.
    It expects non-printable ascii characters to be url-encoded, i.e. a
    get-request would look like:

    ``get%20isis@leap.se%0A``

    and in response, Postfix expects an SMTP-like status code and a string
    describing the nature of or reason for the response, no longer than
    4096 "characters" (which, due to UTF-8 ubiquity, we'll err on the safe
    side and assume that means 4096 bytes.)

    From the Postfix manual on its TCP map protocol
    (http://www.postfix.org/tcp_table.5.html):

       500 SPACE text NEWLINE
              In  case  of  a  lookup request, the requested data
              does not exist.  In case of an update request,  the
              request  was  rejected.   The  text  describes  the
              nature of the problem.

       400 SPACE text NEWLINE
              This  indicates  an  error  condition.   The   text
              describes  the  nature  of  the problem. The client
              should retry the request later.

       200 SPACE text NEWLINE
              The request was successful. In the case of a lookup
              request,  the  text  contains an encoded version of
              the requested data.

    Other SMTP codes: http://www.greenend.org.uk/rjk/tech/smtpreplies.html

    >>> statcodes = StatusCodes()
    >>> if <query local user database for email address>:
    >>>     response_message = statcodes(200)
    >>>     aliasresolver.tellMTA()
    """

    OK    = "OK Others might say 'HELLA AWESOME'...but we're not convinced."
    RETRY = "RETRY Server is busy plotting revolution; requests might take a while."
    BAD   = "BAD bad Leroy Brown, baddest man in the whole...er. Malformed request."
    NOKEY = "NOKEY Couldn't find your keys, sorry. Did you check in the sofa?"
    DEFER = "DEFER_IF_LOCAL xxx fill me in"
    DENY  = "DENY no gurlz aloud in teh tree house."
    FAIL  = "FAIL this belongs on the failblog"

    SMTPCodes = { '200': OK,
                  '400': RETRY,
                  '500': BAD,
                  '550': NOKEY,
                  '552': DEFER,
                  '553': DENY,
                  '554': FAIL, }

    SMTPStrings = { 'OK': 200,
                    'RETRY': 400,
                    'BAD': 500,
                    'NOKEY': 550,
                    'DEFER': 552,
                    'DENY': 553,
                    'FAIL': 554, }

    def __init__(self, status_code=None):
        """Construct an SMTP status code generator.

        :type status_code: str or int
        :param status_code: (optional) see :func:`StatusCode.get`.
        """
        if status_code:
            self.get(status_code)

    def get(self, status_code=None):
        """Takes an SMTP-like status code and returns an SMTP-like message.

        :type status_code: str or int
        :param status_code: The string or integer for the response we want
                            to give back to the MTA, after looking up an
                            email address in the local user database.
                            Can be one of:
            * ``OK``    or ``200``
            * ``RETRY`` or ``400``
            * ``BAD``   or ``500``
            * ``NOKEY`` or ``550``
            * ``DEFER`` or ``552``
            * ``DENY``  or ``553``
            * ``FAIL``  or ``554``
        """
        if status_code:
            if isinstance(status_code, str):
                if status_code.upper() in self.SMTPStrings.keys():
                    return self.SMTPStrings[status_code], getattr(
                        self, status_code.upper(), '')
                else:
                    return 500, self.FAIL
            elif isinstance(status_code, int):
                for k, v in self.SMTPCodes.items():
                    ## we want to return None if it's 500
                    if k == str(status_code) and k != '500':
                        return status_code, v
                log.debug("%s" % self.NOKEY)
                return None, ''


class AliasResolver(postfix.PostfixTCPMapServer):
    """Resolve postfix aliases, similarly to using "$ postmap -q <alias>".

    This class starts a simple LineReceiver server which listens for a string
    specifying an alias ``key`` to look up, which will be used to query the
    local user database. You can test it with:

        $ ./alias_resolver.py &
        $ /usr/bin/postmap -q <key> tcp:localhost:1347

    Resources:
    http://www.postfix.org/proxymap.8.html
    https://www.iana.org/assignments/smtp-enhanced-status-codes/
    """

    virtual_transport = '@example.com'
    use_virtual_transport = False

    def __init__(self, *args, **kwargs):
        """Create a server which listens for Postfix aliases to resolve.

        :param int timeout: Number of seconds to wait for a response.
        :param str delimiter: The delimiter to use for the EOL on responses.
                              (Default: '\n')
        """
        super(postfix.PostfixTCPMapServer, self).__init__(*args, **kwargs)
        self.status_codes = StatusCodes()

    def sendCode(self, code, message=None):
        """Send an SMTP-like code with a message.

        :type code: str or int
        :param code: The status code to send, see
                     ``alias_resolver.StatusCodes``.
        """
        try:
            assert isinstance(code, int), "status code must be type int"
        except AssertionError as ae:
            log.err(ae.message)
            self.sendLine('500 internal server error: %s' % ae.message)

        msg = self.status_codes.get(code)
        if message is not None and isinstance(message, str):
            msg += (" " + message)
        self.sendLine('%3.3d %s' % (code, message or ''))

    def do_get(self, key):
        """Make a query to resolve an alias."""
        if key is None:
            self.sendCode(500)
            log.warn("Command 'get' takes one parameter.")
        else:
            d = defer.maybeDeferred(self.factory.get, key)
            d.addCallbacks(self._cbGot, self._cbNot)
            d.addErrback(log.err)

    @defer.inlineCallbacks
    def do_put(self, keyAndValue):
        """Add a key and value to the database, provided it does not exist.

        :param str keyAndValue: An alias and email address, separated by a
                                space, i.e. ``"isis isis@leap.se"``.
        """
        if keyAndValue is None:
            self.sendCode(500)
            log.warn("Command 'put' takes two parameters.")
        else:
            try:
                key, value = keyAndValue.split(None, 1)
            except ValueError:
                self.sendCode(500)
                log.warn("Command 'put' takes two parameters.")
            else:
                alreadyThere = yield self.do_query(key)
                if alreadyThere is None:
                    d = defer.maybeDeferred(self.factory.put, key, value)
                    d.addCallbacks(self._cbPut, self._cbPout)
                    d.addCallbacks(log.err)
                else:
                    self.sendCode(553)

    @defer.inlineCallbacks
    def do_delete(self, key):
        """Delete an alias from the CouchDB.

        xxx I'm not sure if implementing this would be a good idea...

        :param str key: An email address to delete from the CouchDB.
        """
        raise NotImplemented

    def check_recipient_access(self, key):
        """Make a query to the CouchDB to resolve an alias.

        If the ``key`` is an email address which the CouchDB has information
        for that account, we should respond to Postfix with an '200%20\n".

        :param str key: An email address to look up in the CouchDB.
        """
        return self.do_get(key)

    def virtual_alias_map(self, key):
        """Get the Universal Unique ID for the alias address. If
        virtual_transport is True, then suffix the UUID with a domain.

        xxx I don't think we actually need couchdb for this, the UUID is an
        identifier, not an authenticator. And the SHA1 should always be the
        same, so unless it's considered to expensive to compute (less than
        querying a database, I would presume), it seems silly to do this.

        Instead, we should query CouchDB with the UUID to get the GPG keyid.

        xxx Or are we supposed to query Soledad for this?

        :param str key: An email address to look up in the CouchDB.
        :returns: The UUID of the user.
        """
        ## xxx need email address parser
        userid = createUUID(key)

        if self.use_virtual_transport \
                and isinstance(self.virtual_transport, str):
            return userid.get_urn() + self.virtual_transport
        else:
            return userid.get_urn()


class AliasResolverFactory(postfix.PostfixTCPMapDeferringDictServerFactory):
    """A Factory for creating :class:`AliasResolver` servers, which handles
    inputs and outputs, and keeps an in-memory mapping of Postfix aliases in
    the form of a dictionary.

    >>> from leap.mx import alias_resolver
    >>> aliasResolverFactory = alias_resolver.AliasResolver(
    ...     data={'isis': 'isis@leap.se',
    ...           'drebs': 'drebs@leap.se',
    ...           'elijah': 'elijah@leap.se',})
    >>> aliasResolver = aliasResolverFactory.buildProtocol()
    >>> aliasResolver.check_recipient_access('isis')
    """
    protocol = AliasResolver
    database = couchdb.ConnectedCouchDB

    def __init__(self, addr='127.0.0.1', port=4242, timeout=120,
                 data=None, virtual_transport=None, use_virtual_transport=False,
                 couch_host=None, couch_port=None, couch_dbname='users',
                 couch_username=None, couch_password=None):
        """Create a Factory which returns :class:`AliasResolver` servers.

        :param str addr: A string giving the IP address of this server, for
                         talking to postfix.  Default: '127.0.0.1'
        :param int port: An integer that specifies the port number that this
                         server should listen and respond on, for talking to
                         Postfix.  on. Default: 4242
        :param int timeout: An integer specifying the number of seconds to wait
                            until we should time out. Default: 120
        :param dict data: A dict to use to initialise or update the alias
                          mapping.
        :param str virtual_transport: The domain portion of an email address
                                      to suffix the UUID responses of
                                      ``AliasResolver.virtual_alias_map`` with.
        :param bool use_virtual_transport: If True, suffix UUIDs with the
                                           ``virtual_transport`` string.

        :param str couch_host: The IP address of the CouchDB server to query.
        :param int couch_port: The port of the CouchDB server to query.
        :param str couch_dbname: The database in the CouchDB to bind to.
        :param str couch_username: The username for authenticating to the
                                   CouchDB.
        :param str couch_password: The password for authentication.
        """
        super(postfix.PostfixTCPMapDeferringDictServerFactory,
              self).__init__(data=data)
        self.timeout = timeout
        self.virtual_transport = virtual_transport
        self.use_virtual_transport = use_virtual_transport
        self.noisy = True if config.advanced.noisy else False

        if couch_port is None:
            couch_port = 5984
        if couch_dbname is None:
            couch_dbname = 'users'
        self.database_connected = False
        if couch_host is not None:
            self.couch = self.connectDatabase(couch_host, couch_port,
                                              couch_dbname, couch_username,
                                              couch_password)

        try:
            assert isinstance(port, int), "Port number must be an integer"
            assert isinstance(timeout, int), "Timeout must be an integer"
        except AssertionError, ae:
            raise SystemExit(ae.message)

        if net.checkIPaddress(addr):
            self.addr = address._IPAddress('TCP', addr, int(port))
        else:
            log.msg("Using default address: 127.0.0.1:%s" % port)
            self.addr = address._IPAddress('TCP', '127.0.0.1', int(port))

        log.msg("To configure Postfix to query this alias_resolver,")
        log.msg("you should do:")
        log.msg("    $ postconf -e 'check_recipient_access = tcp:%s:%d"
                % (addr, port))

    def buildProtocol(self):
        """Create an instance of the :class:`AliasResolver` server."""
        proto = self.protocol()
        proto.timeout = self.timeout
        proto.virtual_transport = self.virtual_transport
        proto.use_virtual_transport = self.use_virtual_transport
        proto.factory = self
        return proto

    def _cb_connectDatabase(self):
        self.database_connected = True

    def connectDatabase(self, couch_host, couch_port=None, couch_dbname=None,
                        couch_username=None, couch_password=None):
        """Connect to the CouchDB instance."""
        if not self.database_connected:
            d = self.database(couch_host, couch_port, dbName=couch_dbname,
                              username=couch_username, password=couch_password)
            d.addCallback(self._cb_connectDatabase)
            d.addErrback(log.err)
            return d
        else:
            return self.couch ## xxx are we sure we only want one connection?

    def get(self, key, **kwargs):
        """Query the CouchDB for a user's info.

        :param str key: The alias to look up. Should be either an email address
                         or a username. (xxx do we want to also support lookups
                         by UUID?)
        """
        if self.database_connected:
            return self.couch.queryByEmailOrAlias(key)
        else:
            raise DatabaseNotConnected("Must be connected to a database.")

    def put(self, key, **kwargs):
        """Add an alias to the CouchDB database."""
        raise NotImplemented


if __name__ == "__main__":

    print "To test alias_resolver.py, please use /test/test_alias_resolver.py"

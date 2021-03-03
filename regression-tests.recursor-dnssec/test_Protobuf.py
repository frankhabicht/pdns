import dns
import dnsmessage_pb2
import os
import socket
import struct
import sys
import threading
import time

# Python2/3 compatibility hacks
try:
  from queue import Queue
except ImportError:
  from Queue import Queue

try:
  range = xrange
except NameError:
  pass

from recursortests import RecursorTest

def ProtobufConnectionHandler(queue, conn):
    data = None
    while True:
        data = conn.recv(2)
        if not data:
            break
        (datalen,) = struct.unpack("!H", data)
        data = conn.recv(datalen)
        if not data:
            break

        queue.put(data, True, timeout=2.0)

    conn.close()

def ProtobufListener(queue, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except socket.error as e:
        print("Error binding in the protobuf listener: %s" % str(e))
        sys.exit(1)

    sock.listen(100)
    while True:
        try:
            (conn, _) = sock.accept()
            thread = threading.Thread(name='Connection Handler',
                                      target=ProtobufConnectionHandler,
                                      args=[queue, conn])
            thread.setDaemon(True)
            thread.start()

        except socket.error as e:
            print('Error in protobuf socket: %s' % str(e))

    sock.close()


class ProtobufServerParams:
  def __init__(self, port):
    self.queue = Queue()
    self.port = port

protobufServersParameters = [ProtobufServerParams(4243), ProtobufServerParams(4244)]
protobufListeners = []
for param in protobufServersParameters:
  listener = threading.Thread(name='Protobuf Listener', target=ProtobufListener, args=[param.queue, param.port])
  listener.setDaemon(True)
  listener.start()
  protobufListeners.append(listener)

class TestRecursorProtobuf(RecursorTest):

    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"})
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)

    _auth_zones = {
        '8': {'threads': 1,
              'zones': ['ROOT']},
        '9': {'threads': 1,
              'zones': ['secure.example', 'islandofsecurity.example']},
        '10': {'threads': 1,
               'zones': ['example']},
        '18': {'threads': 1,
               'zones': ['example']}
    }

    def getFirstProtobufMessage(self, retries=1, waitTime=1):
        msg = None

        print("in getFirstProtobufMessage")
        for param in protobufServersParameters:
          print(param.port)
          failed = 0

          while param.queue.empty:
            print(failed)
            print(retries)
            if failed >= retries:
              break

            failed = failed + 1
            print("waiting")
            time.sleep(waitTime)

          self.assertFalse(param.queue.empty())
          data = param.queue.get(False)
          self.assertTrue(data)
          oldmsg = msg
          msg = dnsmessage_pb2.PBDNSMessage()
          msg.ParseFromString(data)
          if oldmsg is not None:
            self.assertEquals(msg, oldmsg)

        print(msg)
        return msg

    def checkNoRemainingMessage(self):
        for param in protobufServersParameters:
          self.assertTrue(param.queue.empty())

    def checkProtobufBase(self, msg, protocol, query, initiator, normalQueryResponse=True, expectedECS=None, receivedSize=None):
        self.assertTrue(msg)
        self.assertTrue(msg.HasField('timeSec'))
        self.assertTrue(msg.HasField('socketFamily'))
        self.assertEquals(msg.socketFamily, dnsmessage_pb2.PBDNSMessage.INET)
        self.assertTrue(msg.HasField('from'))
        fromvalue = getattr(msg, 'from')
        self.assertEquals(socket.inet_ntop(socket.AF_INET, fromvalue), initiator)
        self.assertTrue(msg.HasField('socketProtocol'))
        self.assertEquals(msg.socketProtocol, protocol)
        self.assertTrue(msg.HasField('messageId'))
        self.assertTrue(msg.HasField('serverIdentity'))
        self.assertTrue(msg.HasField('id'))
        self.assertEquals(msg.id, query.id)
        self.assertTrue(msg.HasField('inBytes'))
        if normalQueryResponse:
            # compare inBytes with length of query/response
            # Note that for responses, the size we received might differ
            # because dnspython might compress labels differently from
            # the recursor
            if receivedSize:
                self.assertEquals(msg.inBytes, receivedSize)
            else:
                self.assertEquals(msg.inBytes, len(query.to_wire()))
        if expectedECS is not None:
            self.assertTrue(msg.HasField('originalRequestorSubnet'))
            # v4 only for now
            self.assertEquals(len(msg.originalRequestorSubnet), 4)
            self.assertEquals(socket.inet_ntop(socket.AF_INET, msg.originalRequestorSubnet), '127.0.0.1')

    def checkOutgoingProtobufBase(self, msg, protocol, query, initiator, length=None):
        self.assertTrue(msg)
        self.assertTrue(msg.HasField('timeSec'))
        self.assertTrue(msg.HasField('socketFamily'))
        self.assertEquals(msg.socketFamily, dnsmessage_pb2.PBDNSMessage.INET)
        self.assertTrue(msg.HasField('socketProtocol'))
        self.assertEquals(msg.socketProtocol, protocol)
        self.assertTrue(msg.HasField('messageId'))
        self.assertTrue(msg.HasField('serverIdentity'))
        self.assertTrue(msg.HasField('id'))
        self.assertNotEquals(msg.id, query.id)
        self.assertTrue(msg.HasField('inBytes'))
        if length is not None:
          self.assertEquals(msg.inBytes, length)
        else:
          # compare inBytes with length of query/response
          self.assertEquals(msg.inBytes, len(query.to_wire()))

    def checkProtobufQuery(self, msg, protocol, query, qclass, qtype, qname, initiator='127.0.0.1'):
        self.assertEquals(msg.type, dnsmessage_pb2.PBDNSMessage.DNSQueryType)
        self.checkProtobufBase(msg, protocol, query, initiator)
        # dnsdist doesn't fill the responder field for responses
        # because it doesn't keep the information around.
        self.assertTrue(msg.HasField('to'))
        self.assertEquals(socket.inet_ntop(socket.AF_INET, msg.to), '127.0.0.1')
        self.assertTrue(msg.HasField('question'))
        self.assertTrue(msg.question.HasField('qClass'))
        self.assertEquals(msg.question.qClass, qclass)
        self.assertTrue(msg.question.HasField('qType'))
        self.assertEquals(msg.question.qClass, qtype)
        self.assertTrue(msg.question.HasField('qName'))
        self.assertEquals(msg.question.qName, qname)

    def checkProtobufResponse(self, msg, protocol, response, initiator='127.0.0.1', receivedSize=None, vstate=dnsmessage_pb2.PBDNSMessage.VState.Indeterminate):
        self.assertEquals(msg.type, dnsmessage_pb2.PBDNSMessage.DNSResponseType)
        self.checkProtobufBase(msg, protocol, response, initiator, receivedSize=receivedSize)
        self.assertTrue(msg.HasField('response'))
        self.assertTrue(msg.response.HasField('queryTimeSec'))
        self.assertTrue(msg.response.HasField('validationState'))
        self.assertEquals(msg.response.validationState, vstate)

    def checkProtobufResponseRecord(self, record, rclass, rtype, rname, rttl, checkTTL=True):
        self.assertTrue(record.HasField('class'))
        self.assertEquals(getattr(record, 'class'), rclass)
        self.assertTrue(record.HasField('type'))
        self.assertEquals(record.type, rtype)
        self.assertTrue(record.HasField('name'))
        self.assertEquals(record.name, rname)
        self.assertTrue(record.HasField('ttl'))
        if checkTTL:
            self.assertEquals(record.ttl, rttl)
        self.assertTrue(record.HasField('rdata'))

    def checkProtobufPolicy(self, msg, policyType, reason, trigger, hit, kind):
        self.assertEquals(msg.type, dnsmessage_pb2.PBDNSMessage.DNSResponseType)
        self.assertTrue(msg.response.HasField('appliedPolicyType'))
        self.assertTrue(msg.response.HasField('appliedPolicy'))
        self.assertTrue(msg.response.HasField('appliedPolicyTrigger'))
        self.assertTrue(msg.response.HasField('appliedPolicyHit'))
        self.assertTrue(msg.response.HasField('appliedPolicyKind'))
        self.assertEquals(msg.response.appliedPolicy, reason)
        self.assertEquals(msg.response.appliedPolicyType, policyType)
        self.assertEquals(msg.response.appliedPolicyTrigger, trigger)
        self.assertEquals(msg.response.appliedPolicyHit, hit)
        self.assertEquals(msg.response.appliedPolicyKind, kind)

    def checkProtobufTags(self, msg, tags):
        print(tags)
        print('---')
        print(msg.response.tags)
        self.assertEquals(len(msg.response.tags), len(tags))
        for tag in msg.response.tags:
            self.assertTrue(tag in tags)

    def checkProtobufOutgoingQuery(self, msg, protocol, query, qclass, qtype, qname, initiator='127.0.0.1', length=None):
        self.assertEquals(msg.type, dnsmessage_pb2.PBDNSMessage.DNSOutgoingQueryType)
        self.checkOutgoingProtobufBase(msg, protocol, query, initiator, length=length)
        self.assertTrue(msg.HasField('to'))
        self.assertTrue(msg.HasField('question'))
        self.assertTrue(msg.question.HasField('qClass'))
        self.assertEquals(msg.question.qClass, qclass)
        self.assertTrue(msg.question.HasField('qType'))
        self.assertEquals(msg.question.qType, qtype)
        self.assertTrue(msg.question.HasField('qName'))
        self.assertEquals(msg.question.qName, qname)

    def checkProtobufIncomingResponse(self, msg, protocol, response, initiator='127.0.0.1', length=None):
        self.assertEquals(msg.type, dnsmessage_pb2.PBDNSMessage.DNSIncomingResponseType)
        self.checkOutgoingProtobufBase(msg, protocol, response, initiator, length=length)
        self.assertTrue(msg.HasField('response'))
        self.assertTrue(msg.response.HasField('rcode'))
        self.assertTrue(msg.response.HasField('queryTimeSec'))

    def checkProtobufIncomingNetworkErrorResponse(self, msg, protocol, response, initiator='127.0.0.1'):
        self.checkProtobufIncomingResponse(msg, protocol, response, initiator, length=0)
        self.assertEquals(msg.response.rcode, 65536)

    def checkProtobufIdentity(self, msg, requestorId, deviceId, deviceName):
        print(msg)
        self.assertTrue((requestorId == '') == (not msg.HasField('requestorId')))
        self.assertTrue((deviceId == b'') == (not msg.HasField('deviceId')))
        self.assertTrue((deviceName == '') == (not msg.HasField('deviceName')))
        self.assertEquals(msg.requestorId, requestorId)
        self.assertEquals(msg.deviceId, deviceId)
        self.assertEquals(msg.deviceName, deviceName)

    def setUp(self):
        super(TestRecursorProtobuf, self).setUp()
        # Make sure the queue is empty, in case
        # a previous test failed
        for param in protobufServersParameters:
            while not param.queue.empty():
                param.queue.get(False)

    @classmethod
    def generateRecursorConfig(cls, confdir):
        authzonepath = os.path.join(confdir, 'example.zone')
        with open(authzonepath, 'w') as authzone:
            authzone.write("""$ORIGIN example.
@ 3600 IN SOA {soa}
a 3600 IN A 192.0.2.42
tagged 3600 IN A 192.0.2.84
query-selected 3600 IN A 192.0.2.84
answer-selected 3600 IN A 192.0.2.84
types 3600 IN A 192.0.2.84
types 3600 IN AAAA 2001:DB8::1
types 3600 IN TXT "Lorem ipsum dolor sit amet"
types 3600 IN MX 10 a.example.
types 3600 IN SPF "v=spf1 -all"
types 3600 IN SRV 10 20 443 a.example.
cname 3600 IN CNAME a.example.

""".format(soa=cls._SOA))
        super(TestRecursorProtobuf, cls).generateRecursorConfig(confdir)


class ProtobufDefaultTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export queries and response over protobuf.
    """

    _confdir = 'ProtobufDefault'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir

    def testA(self):
        name = 'a.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)

        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP query and answer
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res, '127.0.0.1')
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.42')
        self.checkNoRemainingMessage()

    def testCNAME(self):
        name = 'cname.example.'
        expectedCNAME = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'CNAME', 'a.example.')
        expectedA = dns.rrset.from_text('a.example.', 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        raw = self.sendUDPQuery(query, decode=False)
        res = dns.message.from_wire(raw)
        self.assertRRsetInAnswer(res, expectedCNAME)
        self.assertRRsetInAnswer(res, expectedA)

        # check the protobuf messages corresponding to the UDP query and answer
        # but first let the protobuf messages the time to get there
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res, '127.0.0.1', receivedSize=len(raw))
        self.assertEquals(len(msg.response.rrs), 2)
        rr = msg.response.rrs[0]
        # we don't want to check the TTL for the A record, it has been cached by the previous test
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.CNAME, name, 15)
        self.assertEquals(rr.rdata, b'a.example.')
        rr = msg.response.rrs[1]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, 'a.example.', 15, checkTTL=False)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.42')
        self.checkNoRemainingMessage()

class OutgoingProtobufDefaultTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export outgoing queries over protobuf.
    It must be improved and setup env so we can check for incoming responses, but makes sure for now
    that the recursor at least connects to the protobuf server.
    """

    _confdir = 'OutgoingProtobufDefault'
    _config_template = """
    # Switch off QName Minimization, it generates much more protobuf messages
    # (or make the test much more smart!)
    qname-minimization=no
"""
    _lua_config_file = """
    outgoingProtobufServer({"127.0.0.1:%d", "127.0.0.1:%d"})
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)

    def testA(self):
        name = 'host1.secure.example.'
        expected = list()
        for qname, qtype, proto, size in [
                ('example.', dns.rdatatype.DS, dnsmessage_pb2.PBDNSMessage.UDP, 167),
                (None, None, None, None),  # Query for secure.example.|DS that returns a delegation
                ('secure.example.', dns.rdatatype.DS, dnsmessage_pb2.PBDNSMessage.UDP, 182),
                ('example.', dns.rdatatype.DNSKEY, dnsmessage_pb2.PBDNSMessage.UDP, 219),
                (None, None, None, None),  # Query for host1.secure.example.|DS that returns a delegation
                (None, None, None, None),  # Query for host1.secure.example.|DS that returns a NXQType
                ('secure.example.', dns.rdatatype.DNSKEY, dnsmessage_pb2.PBDNSMessage.UDP, 233),
                ('host1.secure.example.', dns.rdatatype.A, dnsmessage_pb2.PBDNSMessage.UDP, 175),
        ]:
            if not qname:
                expected.append((None, None, None, None, None, None))
                continue
            query = dns.message.make_query(qname, qtype, use_edns=True, want_dnssec=True)
            resp = dns.message.make_response(query)
            expected.append((
                qname, qtype, query, resp, proto, size
            ))

        # expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.RD
        res = self.sendUDPQuery(query)

        for qname, qtype, qry, ans, proto, size in expected:
            if not qname:
                self.getFirstProtobufMessage()
                self.getFirstProtobufMessage()
                continue

            msg = self.getFirstProtobufMessage()
            self.checkProtobufOutgoingQuery(msg, proto, qry, dns.rdataclass.IN, qtype, qname)

            # Check the answer
            msg = self.getFirstProtobufMessage()
            self.checkProtobufIncomingResponse(msg, proto, ans, length=size)

        self.checkNoRemainingMessage()

class OutgoingProtobufNoQueriesTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export incoming responses but not outgoing queries over protobuf.
    It must be improved and setup env so we can check for incoming responses, but makes sure for now
    that the recursor at least connects to the protobuf server.
    """

    _confdir = 'OutgoingProtobufNoQueries'
    _config_template = """
    # Switch off QName Minimization, it generates much more protobuf messages
    # (or make the test much more smart!)
    qname-minimization=no
    auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    outgoingProtobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=false, logResponses=true })
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)

    def testA(self):
        name = 'www.example.org.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.RD
        res = self.sendUDPQuery(query)

        # check the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufIncomingNetworkErrorResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res)
        self.checkNoRemainingMessage()

class ProtobufMasksTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export queries and response over protobuf, respecting the configured initiator masking.
    """

    _confdir = 'ProtobufMasks'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _protobufMaskV4 = 4
    _protobufMaskV6 = 128
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"})
    setProtobufMasks(%d, %d)
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port, _protobufMaskV4, _protobufMaskV6)

    def testA(self):
        name = 'a.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP query and answer
        # but first let the protobuf messages the time to get there
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name, '112.0.0.0')
        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res, '112.0.0.0')
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.42')
        self.checkNoRemainingMessage()

class ProtobufQueriesOnlyTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export queries but not responses over protobuf.
    """

    _confdir = 'ProtobufQueriesOnly'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=true, logResponses=false } )
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)

    def testA(self):
        name = 'a.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf message corresponding to the UDP query
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        # no response
        self.checkNoRemainingMessage()

class ProtobufResponsesOnlyTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export responses but not queries over protobuf.
    """

    _confdir = 'ProtobufResponsesOnly'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=false, logResponses=true } )
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)

    def testA(self):
        name = 'a.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf message corresponding to the UDP response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res)
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.42')
        # nothing else in the queue
        self.checkNoRemainingMessage()

class ProtobufTaggedOnlyTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export queries and responses but only if they have been tagged.
    """

    _confdir = 'ProtobufTaggedOnly'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=true, logResponses=true, taggedOnly=true } )
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)
    _tags = ['tag1', 'tag2']
    _tag_from_gettag = 'tag-from-gettag'
    _lua_dns_script_file = """
    function gettag(remote, ednssubnet, localip, qname, qtype, ednsoptions, tcp)
      if qname:equal('tagged.example.') then
        return 0, { '%s' }
      end
      return 0
    end
    function preresolve(dq)
      if dq.qname:equal('tagged.example.') then
        dq:addPolicyTag('%s')
        dq:addPolicyTag('%s')
      end
      return false
    end
    """ % (_tag_from_gettag, _tags[0], _tags[1])

    def testA(self):
        name = 'a.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf message corresponding to the UDP response
        # the first query and answer are not tagged, so there is nothing in the queue
        time.sleep(1)
        self.checkNoRemainingMessage()

    def testTagged(self):
        name = 'tagged.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.84')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP query and answer
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        self.checkProtobufTags(msg, [ self._tag_from_gettag ])
        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res)
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.84')
        tags = [ self._tag_from_gettag ] + self._tags
        self.checkProtobufTags(msg, tags)
        self.checkNoRemainingMessage()

class ProtobufSelectedFromLuaTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export queries and responses but only if they have been selected from Lua.
    """

    _confdir = 'ProtobufSelectedFromLua'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=false, logResponses=false } )
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)
    _lua_dns_script_file = """
    local ffi = require("ffi")

    ffi.cdef[[
      typedef struct pdns_ffi_param pdns_ffi_param_t;

      const char* pdns_ffi_param_get_qname(pdns_ffi_param_t* ref);
      void pdns_ffi_param_set_log_query(pdns_ffi_param_t* ref, bool logQuery);
    ]]

    function gettag_ffi(obj)
      qname = ffi.string(ffi.C.pdns_ffi_param_get_qname(obj))
      if qname == 'query-selected.example' then
        ffi.C.pdns_ffi_param_set_log_query(obj, true)
      end
      return 0
    end

    function preresolve(dq)
      if dq.qname:equal('answer-selected.example.') then
        dq.logResponse = true
      end
      return false
    end
    """

    def testA(self):
        name = 'a.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf message corresponding to the UDP response
        # the first query and answer are not selected, so there is nothing in the queue
        self.checkNoRemainingMessage()

    def testQuerySelected(self):
        name = 'query-selected.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.84')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP query
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        # there should be no response
        self.checkNoRemainingMessage()

    def testResponseSelected(self):
        name = 'answer-selected.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.84')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res)
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.84')
        self.checkNoRemainingMessage()

class ProtobufExportTypesTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export other types than A, AAAA and CNAME over protobuf.
    """

    _confdir = 'ProtobufExportTypes'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { exportTypes={"AAAA", "MX", "SPF", "SRV", "TXT"} } )
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)

    def testA(self):
        name = 'types.example.'
        expected = [dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.84'),
                    dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'AAAA', '2001:DB8::1'),
                    dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'MX', '10 a.example.'),
                    dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'SPF', '"v=spf1 -all"'),
                    dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'SRV', '10 20 443 a.example.'),
                    dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'TXT', '"Lorem ipsum dolor sit amet"'),
                   ]
        query = dns.message.make_query(name, 'ANY', want_dnssec=True)
        query.flags |= dns.flags.CD
        raw = self.sendUDPQuery(query, decode=False)
        res = dns.message.from_wire(raw)

        for rrset in expected:
            self.assertRRsetInAnswer(res, rrset)

        # check the protobuf messages corresponding to the UDP query and answer
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res, '127.0.0.1', receivedSize=len(raw))
        self.assertEquals(len(msg.response.rrs), 5)
        for rr in msg.response.rrs:
            self.assertTrue(rr.type in [dns.rdatatype.AAAA, dns.rdatatype.TXT, dns.rdatatype.MX, dns.rdatatype.SPF, dns.rdatatype.SRV])

            if rr.type == dns.rdatatype.AAAA:
                self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.AAAA, name, 15)
                self.assertEquals(socket.inet_ntop(socket.AF_INET6, rr.rdata), '2001:db8::1')
            elif rr.type == dns.rdatatype.TXT:
                self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.TXT, name, 15)
                self.assertEquals(rr.rdata, b'"Lorem ipsum dolor sit amet"')
            elif rr.type == dns.rdatatype.MX:
                self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.MX, name, 15)
                self.assertEquals(rr.rdata, b'a.example.')
            elif rr.type == dns.rdatatype.SPF:
                self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.SPF, name, 15)
                self.assertEquals(rr.rdata, b'"v=spf1 -all"')
            elif rr.type == dns.rdatatype.SRV:
                self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.SRV, name, 15)
                self.assertEquals(rr.rdata, b'a.example.')

        self.checkNoRemainingMessage()

class ProtobufTaggedExtraFieldsTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export extra fields that may have been set while being tagged.
    """

    _confdir = 'ProtobufTaggedExtraFields'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=true, logResponses=true } )
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)
    _requestorId = 'S-000001727'
    _deviceId = 'd1:0a:91:dc:cc:82'
    _deviceName = 'Joe'
    _lua_dns_script_file = """
    function gettag(remote, ednssubnet, localip, qname, qtype, ednsoptions, tcp)
      if qname:equal('tagged.example.') then
        -- tag number, policy tags, data, requestorId, deviceId, deviceName
        return 0, {}, {}, '%s', '%s', '%s'
      end
      return 0
    end
    """ % (_requestorId, _deviceId, _deviceName)

    def testA(self):
        name = 'a.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf message corresponding to the UDP response
        # the first query and answer are not tagged, so there is nothing in the queue
        # check the protobuf messages corresponding to the UDP query and answer
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        self.checkProtobufIdentity(msg, '', b'', '')

        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res, '127.0.0.1')
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.42')
        self.checkProtobufIdentity(msg, '', b'', '')
        self.checkNoRemainingMessage()

    def testTagged(self):
        name = 'tagged.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.84')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP query and answer
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)
        self.checkProtobufIdentity(msg, self._requestorId, self._deviceId.encode('ascii'), self._deviceName)

        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res)
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.84')
        self.checkProtobufIdentity(msg, self._requestorId, self._deviceId.encode('ascii'), self._deviceName)
        self.checkNoRemainingMessage()

class ProtobufTaggedExtraFieldsFFITest(ProtobufTaggedExtraFieldsTest):
    """
    This test makes sure that we correctly export extra fields that may have been set while being tagged (FFI version).
    """
    _confdir = 'ProtobufTaggedExtraFieldsFFI'
    _config_template = """
auth-zones=example=configs/%s/example.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=true, logResponses=true } )
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port)
    _lua_dns_script_file = """
    local ffi = require("ffi")

    ffi.cdef[[
      typedef struct pdns_ffi_param pdns_ffi_param_t;

      const char* pdns_ffi_param_get_qname(pdns_ffi_param_t* ref);
      void pdns_ffi_param_set_tag(pdns_ffi_param_t* ref, unsigned int tag);
      void pdns_ffi_param_set_requestorid(pdns_ffi_param_t* ref, const char* name);
      void pdns_ffi_param_set_devicename(pdns_ffi_param_t* ref, const char* name);
      void pdns_ffi_param_set_deviceid(pdns_ffi_param_t* ref, size_t len, const void* name);
    ]]

    function gettag_ffi(obj)
      qname = ffi.string(ffi.C.pdns_ffi_param_get_qname(obj))
      if qname == 'tagged.example' then
        ffi.C.pdns_ffi_param_set_requestorid(obj, "%s")
        deviceid = "%s"
        ffi.C.pdns_ffi_param_set_deviceid(obj, string.len(deviceid), deviceid)
        ffi.C.pdns_ffi_param_set_devicename(obj, "%s")
      end
      return 0
    end
    """ % (ProtobufTaggedExtraFieldsTest._requestorId, ProtobufTaggedExtraFieldsTest._deviceId, ProtobufTaggedExtraFieldsTest._deviceName)

class ProtobufRPZTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export the RPZ applied policy in our protobuf messages
    """

    _confdir = 'ProtobufRPZ'
    _config_template = """
auth-zones=example=configs/%s/example.rpz.zone""" % _confdir
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=true, logResponses=true } )
    rpzFile('configs/%s/zone.rpz', { policyName="zone.rpz."})
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port, _confdir)

    @classmethod
    def generateRecursorConfig(cls, confdir):
        authzonepath = os.path.join(confdir, 'example.rpz.zone')
        with open(authzonepath, 'w') as authzone:
            authzone.write("""$ORIGIN example.
@ 3600 IN SOA {soa}
sub.test 3600 IN A 192.0.2.42
""".format(soa=cls._SOA))

        rpzFilePath = os.path.join(confdir, 'zone.rpz')
        with open(rpzFilePath, 'w') as rpzZone:
            rpzZone.write("""$ORIGIN zone.rpz.
@ 3600 IN SOA {soa}
*.test.example.zone.rpz. 60 IN CNAME rpz-passthru.
""".format(soa=cls._SOA))

        super(ProtobufRPZTest, cls).generateRecursorConfig(confdir)

    def testA(self):
        name = 'sub.test.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP query and answer
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)

        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res)
        self.checkProtobufPolicy(msg, dnsmessage_pb2.PBDNSMessage.PolicyType.QNAME, 'zone.rpz.', '*.test.example.', 'sub.test.example', dnsmessage_pb2.PBDNSMessage.PolicyKind.NoAction)
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.42')
        self.checkNoRemainingMessage()

class ProtobufRPZTagsTest(TestRecursorProtobuf):
    """
    This test makes sure that we correctly export the RPZ tags in our protobuf messages
    """

    _confdir = 'ProtobufRPZTags'
    _config_template = """
auth-zones=example=configs/%s/example.rpz.zone""" % _confdir
    _tags = ['tag1', 'tag2']
    _tags_from_gettag = ['tag1-from-gettag', 'tag2-from-gettag']
    _tags_from_rpz = ['tag1-from-rpz', 'tag2-from-rpz' ]
    _lua_config_file = """
    protobufServer({"127.0.0.1:%d", "127.0.0.1:%d"}, { logQueries=true, logResponses=true, tags={'tag1', 'tag2'} } )
    rpzFile('configs/%s/zone.rpz', { policyName="zone.rpz.", tags={ '%s', '%s'} })
    """ % (protobufServersParameters[0].port, protobufServersParameters[1].port, _confdir, _tags_from_rpz[0], _tags_from_rpz[1])
    _lua_dns_script_file = """
    function gettag(remote, ednssubnet, localip, qname, qtype, ednsoptions, tcp)
      return 0, { '%s', '%s' }
    end
    function preresolve(dq)
      dq:addPolicyTag('%s')
      dq:addPolicyTag('%s')
      return false
    end
    """ % (_tags_from_gettag[0], _tags_from_gettag[1], _tags[0], _tags[1])

    @classmethod
    def generateRecursorConfig(cls, confdir):
        authzonepath = os.path.join(confdir, 'example.rpz.zone')
        with open(authzonepath, 'w') as authzone:
            authzone.write("""$ORIGIN example.
@ 3600 IN SOA {soa}
sub.test 3600 IN A 192.0.2.42
""".format(soa=cls._SOA))

        rpzFilePath = os.path.join(confdir, 'zone.rpz')
        with open(rpzFilePath, 'w') as rpzZone:
            rpzZone.write("""$ORIGIN zone.rpz.
@ 3600 IN SOA {soa}
*.test.example.zone.rpz. 60 IN CNAME rpz-passthru.
""".format(soa=cls._SOA))

        super(ProtobufRPZTagsTest, cls).generateRecursorConfig(confdir)

    def testA(self):
        name = 'sub.test.example.'
        expected = dns.rrset.from_text(name, 0, dns.rdataclass.IN, 'A', '192.0.2.42')
        query = dns.message.make_query(name, 'A', want_dnssec=True)
        query.flags |= dns.flags.CD
        res = self.sendUDPQuery(query)
        self.assertRRsetInAnswer(res, expected)

        # check the protobuf messages corresponding to the UDP query and answer
        msg = self.getFirstProtobufMessage()
        self.checkProtobufQuery(msg, dnsmessage_pb2.PBDNSMessage.UDP, query, dns.rdataclass.IN, dns.rdatatype.A, name)

        # then the response
        msg = self.getFirstProtobufMessage()
        self.checkProtobufResponse(msg, dnsmessage_pb2.PBDNSMessage.UDP, res)
        self.checkProtobufPolicy(msg, dnsmessage_pb2.PBDNSMessage.PolicyType.QNAME, 'zone.rpz.', '*.test.example.', 'sub.test.example', dnsmessage_pb2.PBDNSMessage.PolicyKind.NoAction)
        self.checkProtobufTags(msg, self._tags + self._tags_from_gettag + self._tags_from_rpz)
        self.assertEquals(len(msg.response.rrs), 1)
        rr = msg.response.rrs[0]
        # we have max-cache-ttl set to 15
        self.checkProtobufResponseRecord(rr, dns.rdataclass.IN, dns.rdatatype.A, name, 15)
        self.assertEquals(socket.inet_ntop(socket.AF_INET, rr.rdata), '192.0.2.42')
        self.checkNoRemainingMessage()

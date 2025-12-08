import threading
import time
from dnslib import DNSRecord, QTYPE, RR, A
from dnslib.server import DNSServer, DNSHandler, BaseResolver, DNSLogger
from bottle import route, run, request

# Configuration
DYNAMODB_DOMAIN = "dynamodb.us-east-1.amazonaws.com."
DYNAMODB_IP = "172.20.0.3" # IP of dynamodb container
TTL = 1

class CustomResolver(BaseResolver):
    def __init__(self):
        self.healthy = True
        self.lock = threading.Lock()

    def resolve(self, request, handler):
        reply = request.reply()
        qname = request.q.qname
        qtype = request.q.qtype
        
        # Log query
        print(f"Query: {qname} {QTYPE[qtype]}")
        
        # Log to metrics
        import time
        status = "success"

        if str(qname) == DYNAMODB_DOMAIN:
            with self.lock:
                if not self.healthy:
                    # Simulate failure: Return NXDOMAIN or just empty answer
                    reply.header.rcode = 3 # NXDOMAIN
                    status = "nxdomain"
                else:
                    # Healthy: Return A record
                    reply.add_answer(RR(qname, QTYPE.A, rdata=A(DYNAMODB_IP), ttl=TTL))
        
        # Log metrics
        try:
            with open("/metrics/dns_server.csv", "a") as f:
                f.write(f"{time.time()},{status}\n")
        except Exception as e:
            print(f"Failed to write metrics: {e}")
            pass
        
        return reply

    def set_health(self, healthy: bool):
        with self.lock:
            self.healthy = healthy
            print(f"DNS Health set to: {self.healthy}")

resolver = CustomResolver()

# Management API
@route('/break_dns', method='POST')
def break_dns():
    resolver.set_health(False)
    return {"status": "broken", "message": "DNS for DynamoDB is now failing"}

@route('/fix_dns', method='POST')
def fix_dns():
    resolver.set_health(True)
    return {"status": "fixed", "message": "DNS for DynamoDB is now healthy"}

def start_dns():
    logger = DNSLogger(prefix=False)
    server = DNSServer(resolver, port=53, address="0.0.0.0", logger=logger)
    server.start_thread()
    print("DNS Server started on port 53")

if __name__ == "__main__":
    # Start DNS Server in background thread
    start_dns()
    
    # Start Management API
    run(host='0.0.0.0', port=8080)

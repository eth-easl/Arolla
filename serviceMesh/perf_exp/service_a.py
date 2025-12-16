from flask import Flask
import requests
import os

app = Flask(__name__)

@app.route('/')
def hello():
    try:
        # Service A calls Service B
        # In K8s, service-b is the DNS name
        resp = requests.get("http://service-b")
        return f"Service A -> {resp.text}", resp.status_code
    except Exception as e:
        return f"Service A failed to call Service B: {str(e)}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)

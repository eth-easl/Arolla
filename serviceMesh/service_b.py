from flask import Flask
import random

app = Flask(__name__)

@app.route('/')
def hello():
    # Randomly fail to trigger retries
    # 50% failure rate
    if random.random() < 0.5:
        return "Service B Internal Error", 500
    return "Service B Success", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)

import time
import requests
import sys

def main():
    url = "http://service-a"
    print(f"Starting client, sending requests to {url}...")
    while True:
        try:
            response = requests.get(url)
            print(f"Response: {response.status_code} - {response.text.strip()}")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(1)

if __name__ == "__main__":
    main()

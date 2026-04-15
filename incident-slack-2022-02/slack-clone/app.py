import os
import time
import json
from flask import Flask, jsonify, make_response
from pymemcache.client import base
import pymysql

app = Flask(__name__)

DB_HOST = os.getenv("DB_HOST", "mysql-db")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "thisisarandompassword")
DB_NAME = os.getenv("DB_NAME", "slack_clone")
CACHE_HOST = os.getenv("CACHE_HOST", "mcrouter-svc:5000")


def get_cache_client():
    host, port = CACHE_HOST.split(":")
    return base.Client((host, int(port)))


def init_db():
    """
    Initalize the database with
    one table `messages`, and fill the table
    with 1000 rows of the type
    <id: int, content: varchar(255)>
    """
    print("Initializing database...")
    while True:
        try:
            connection = pymysql.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASSWORD, cursorclass=pymysql.cursors.DictCursor
            )
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
            connection.close()

            connection = pymysql.connect(
                host=DB_HOST,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                cursorclass=pymysql.cursors.DictCursor,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        content VARCHAR(255)
                    )
                """
                )

                cursor.execute("SELECT COUNT(*) as count FROM messages")
                result = cursor.fetchone()
                if result["count"] == 0:
                    print("Seeding database with 1000 messages...")
                    messages = [(f"This is test message {i}",) for i in range(1, 1001)]
                    cursor.executemany("INSERT INTO messages (content) VALUES (%s)", messages)
                    connection.commit()
            connection.close()
            print("Database initialization complete.")
            break
        except Exception as e:
            print(f"Error initializing DB: {e}. Retrying in 5 seconds...")
            time.sleep(5)


init_db()


@app.route("/api/message/<int:message_id>", methods=["GET"])
def get_message(message_id):
    client = get_cache_client()
    cache_key = str(message_id)

    cached_content = client.get(cache_key)
    if cached_content:
        response = make_response(jsonify({"id": message_id, "content": cached_content.decode("utf-8")}))
        response.headers["X-Cache"] = "HIT"  # Used by CDNs apparently
        return response

    try:
        connection = pymysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME, cursorclass=pymysql.cursors.DictCursor
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT content FROM messages WHERE id = %s", (message_id,))
            result = cursor.fetchone()

        connection.close()

        if result:
            content = result["content"]
            client.set(cache_key, content, expire=600)

            response = make_response(jsonify({"id": message_id, "content": content}))
            response.headers["X-Cache"] = "MISS"
            return response
        else:
            return jsonify({"error": "Message not found"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

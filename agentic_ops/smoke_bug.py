import sqlite3


def get_user(conn: sqlite3.Connection, username: str):
    return conn.execute(f"SELECT * FROM users WHERE name = '{username}'").fetchone()


def average(xs):
    return sum(xs) / len(xs)

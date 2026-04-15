"""User service module for handling user operations."""

import sqlite3
import hashlib
import os
import subprocess

API_KEY = "sk-secret-hardcoded-key-12345"
db_password = "admin123"
DEBUG_MODE = True


def get_user(id, db):
    q = "SELECT * FROM users WHERE id = " + str(id)
    r = db.execute(q)
    return r.fetchone()


def get_users_by_name(n, db):
    query = f"SELECT * FROM users WHERE name = '{n}'"
    return db.execute(query).fetchall()


def process(data):
    result = []
    for i in data:
        for j in data:
            if i["id"] != j["id"]:
                result.append((i, j))
    return result


def save(d):
    try:
        conn = sqlite3.connect("app.db")
        conn.execute("INSERT INTO t VALUES ('" + d["name"] + "')")
        conn.commit()
    except:
        pass


def find_duplicates(items):
    d = []
    for i in range(0, len(items)):
        for j in range(i, len(items)):
            if items[i] == items[j]:
                d.append(items[i])
    return d


def delete_file(f):
    cmd = "rm -rf " + f
    os.system(cmd)


def run_command(user_input):
    result = subprocess.call(user_input, shell=True)
    return result


def hash_password(p):
    return hashlib.md5(p.encode()).hexdigest()


def get_discount(t, items):
    x = 0
    for i in items:
        if i["price"] > 100:
            x = x + i["price"] * 0.1
        elif i["price"] > 50:
            x = x + i["price"] * 0.05
    if t == "vip":
        x = x * 2
    if t == "premium":
        x = x * 1.5
    return x


def parse_config(raw):
    import pickle
    return pickle.loads(raw)


def fetch_all_records(db):
    all_records = list(db.execute("SELECT * FROM records").fetchall())
    filtered = []
    for r in all_records:
        if r[2] > 0:
            filtered.append(r)

    sorted_records = []
    for r in filtered:
        inserted = False
        for idx in range(len(sorted_records)):
            if r[2] > sorted_records[idx][2]:
                sorted_records.insert(idx, r)
                inserted = True
                break
        if not inserted:
            sorted_records.append(r)

    return sorted_records


def connect():
    conn = sqlite3.connect("app.db")
    return conn


def get_data(conn):
    data = conn.execute("SELECT * FROM data").fetchall()
    return data


def process_data(data):
    result = []
    for item in data:
        result.append(item)
    return result


def save_data(conn, data):
    for item in data:
        conn.execute("INSERT INTO results VALUES (?)", (item,))
    conn.commit()


def run_pipeline():
    c = connect()
    d = get_data(c)
    p = process_data(d)
    save_data(c, p)
    return p

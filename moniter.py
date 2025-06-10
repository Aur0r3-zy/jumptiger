#!/usr/bin/env python
# -*- coding: utf-8 -*-

from flask import Flask, jsonify, render_template, request
import requests
import time

app = Flask(__name__)

# 配置 server.py 的 API 地址
SERVER_API_URL = "http://127.0.0.1:8388/api/stats"

@app.route("/")
def index():
    """渲染监控页面"""
    return render_template("index.html")

@app.route("/api/stats")
def fetch_stats():
    """从 server.py 获取统计数据"""
    try:
        response = requests.get(SERVER_API_URL)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to fetch stats from server.py"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8088, debug=True)
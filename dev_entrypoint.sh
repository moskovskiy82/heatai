#!/bin/sh
pip install --no-cache-dir -r requirements.txt
exec watchmedo auto-restart --pattern="*.py;*.yaml" --recursive -- python heatai.py

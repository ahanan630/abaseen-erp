import streamlit as st
import pandas as pd
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal, getcontext, ROUND_HALF_UP
from fpdf import FPDF
from io import BytesIO
import hashlib
import hmac
import json
from contextlib import contextmanager
import shutil
import re
import time
import logging
import os
import secrets
from typing import Optional
from urllib.parse import quote

from orders_module import init_orders_tables, show_orders_page

# NOTE: This is a copy of the app entrypoint for GitHub deployment folder.
# Keep the main `app.py` in repo root as the canonical source.

# ==================== CONFIGURATION ====================
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "business.db"

MASTER_DB_PATH = DATA_DIR / "companies.db"

COMPANY_NAME = "Abaseen General Order Suppliers"
SECOND_COMPANY_NAME = "AAA Trusted Enterprises"
NTN_NUMBER = "4252463-6"

# For deployment, the rest of the app code remains in the root `app.py`.
# This file simply exists as a convenience copy for GitHub deployment packaging.

st.write("This copy is a deployment placeholder. Use the root app.py instead.")

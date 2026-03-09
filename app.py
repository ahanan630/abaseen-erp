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

# ==================== CONFIGURATION ====================
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "business.db"

# Master DB to track companies (each company has its own SQLite database file)
MASTER_DB_PATH = DATA_DIR / "companies.db"

COMPANY_NAME = "Abaseen General Order Suppliers"
SECOND_COMPANY_NAME = "AAA Trusted Enterprises"
NTN_NUMBER = "4252463-6"


def get_company_logo_path(company_name: str) -> Path:
    """Return a logo path for the given company (falls back to default logo)."""
    company_name = (company_name or "").strip()
    logo_map = {
        COMPANY_NAME: "abaseen logo.png",
        SECOND_COMPANY_NAME: "aaa logo.png",
    }

    # Default/fallback logo
    default_logo = BASE_DIR / "abaseen logo.png"
    candidate = BASE_DIR / logo_map.get(company_name, "") if company_name in logo_map else None
    if candidate and candidate.exists():
        return candidate
    return default_logo


def _slugify_company_name(name: str) -> str:
    """Create a safe filename slug for a company database."""
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "company"


@contextmanager
def get_master_db():
    """Connection to the master registry DB (companies list)."""
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(MASTER_DB_PATH, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()


def init_master_db():
    with get_master_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                CompanyID TEXT PRIMARY KEY,
                CompanyName TEXT NOT NULL UNIQUE,
                DbFile TEXT NOT NULL UNIQUE,
                CreatedAt TEXT NOT NULL,
                IsActive TEXT NOT NULL DEFAULT 'Yes'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_companies_active ON companies(IsActive)")


def list_companies(active_only: bool = True):
    """Return list of companies as dicts: {id, name, db_file}."""
    init_master_db()
    with get_master_db() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT CompanyID, CompanyName, DbFile FROM companies WHERE IsActive = 'Yes' ORDER BY CompanyName"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT CompanyID, CompanyName, DbFile FROM companies ORDER BY CompanyName"
            ).fetchall()
    return [{"id": r[0], "name": r[1], "db_file": r[2]} for r in rows]


def get_company_by_name(company_name: str):
    init_master_db()
    with get_master_db() as conn:
        row = conn.execute(
            "SELECT CompanyID, CompanyName, DbFile FROM companies WHERE CompanyName = ?",
            (company_name.strip(),),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "db_file": row[2]}


def get_company_db_path(db_file: str) -> Path:
    return DATA_DIR / db_file


def get_active_company_name() -> str:
    try:
        return st.session_state.get("company_name") or COMPANY_NAME
    except Exception:
        return COMPANY_NAME


def get_active_db_path() -> Path:
    """Current active company DB path (falls back to legacy DB_PATH)."""
    try:
        db_file = st.session_state.get("company_db_file")
        if db_file:
            return get_company_db_path(db_file)
    except Exception:
        pass
    return DB_PATH


def set_active_company(company: dict):
    """Persist active company selection into session state."""
    st.session_state.company_id = company["id"]
    st.session_state.company_name = company["name"]
    st.session_state.company_db_file = company["db_file"]


def ensure_company_registered(company_name: str, db_file: str):
    """Ensure a company exists in master registry (does not create DB schema)."""
    init_master_db()
    existing = get_company_by_name(company_name)
    if existing:
        return existing

    company_id = f"CMP-{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_master_db() as conn:
        conn.execute(
            "INSERT INTO companies (CompanyID, CompanyName, DbFile, CreatedAt, IsActive) VALUES (?, ?, ?, ?, 'Yes')",
            (company_id, company_name.strip(), db_file, created_at),
        )
    return {"id": company_id, "name": company_name.strip(), "db_file": db_file}


def create_new_company(company_name: str):
    """Create a brand-new company with its own DB file and initialized schema."""
    company_name = (company_name or "").strip()
    if not company_name:
        raise ValueError("Company name is required")
    if len(company_name) > 80:
        raise ValueError("Company name is too long")

    # Prevent duplicates by name
    existing = get_company_by_name(company_name)
    if existing:
        return existing

    slug = _slugify_company_name(company_name)
    base_file = f"company_{slug}.db"
    db_file = base_file
    counter = 2
    while (DATA_DIR / db_file).exists():
        db_file = f"company_{slug}_{counter}.db"
        counter += 1

    # Register then initialize DB schema
    company = ensure_company_registered(company_name, db_file)
    init_db(db_path=get_company_db_path(db_file))
    return company


def bootstrap_company_registry():
    """Ensure master registry contains the legacy company and AAA Trusted Enterprises."""
    init_master_db()

    # Register the existing/legacy DB as a company so users can select it.
    ensure_company_registered(COMPANY_NAME, DB_PATH.name)
    # Ensure legacy schema is up to date
    init_db(db_path=DB_PATH)

    # Ensure requested company exists (fresh DB if missing)
    try:
        create_new_company(SECOND_COMPANY_NAME)
    except Exception:
        logging.exception("bootstrap_company_registry: failed creating AAA Trusted Enterprises")

# Password hashing
PASSWORD_SALT = b"abaseen_salt_2026"
PASSWORD_ITERATIONS = 200_000
DEFAULT_ADMIN_PASSWORD = "Admin_786"

# ==================== TRANSLATIONS ====================
TRANSLATIONS = {
    'English': {
        # Navigation & Main
        'dashboard': 'Dashboard',
        'reports': 'Reports',
        'sales': 'Sales',
        'purchases': 'Purchases',
        'orders': 'Orders',
        'inventory': 'Inventory',
        'expenses': 'Expenses',
        'loans': 'Loans',
        'payments': 'Payments',
        'customers': 'Customers',
        'vendors': 'Vendors',
        'admin': 'Admin',
        'backup_export': 'Backup & Export',
        'logout': 'Logout',

        # Share
        'share': 'Share',
        'whatsapp': 'WhatsApp',
        'email': 'Email',
        
        # Dashboard
        'business_dashboard': 'Business Dashboard',
        'select_month': 'Select Month',
        'language': 'Language',
        'for_the_period': 'For the period',
        'to': 'to',
        'total_sales': 'Total Sales',
        'cogs': 'COGS (Cost of Goods Sold)',
        'total_expenses': 'Total Expenses',
        'net_profit': 'Net Profit',
        'monthly_trend': 'Monthly Trend',
        'recent_activity': 'Recent Activity',
        'recent_invoices': 'Recent Invoices',
        'recent_bills': 'Recent Bills',
        'recent_expenses': 'Recent Expenses',
        'no_data': 'No data available',
        'loans_given': 'Loans Given',
        'loans_received': 'Loans Received',
        
        # Common
        'date': 'Date',
        'customer': 'Customer',
        'vendor': 'Vendor',
        'amount': 'Amount',
        'total': 'Total',
        'status': 'Status',
        'actions': 'Actions',
        'description': 'Description',
        'save': 'Save',
        'cancel': 'Cancel',
        'delete': 'Delete',
        'edit': 'Edit',
        'view': 'View',
        'add': 'Add',
        'update': 'Update',
        'submit': 'Submit',
        'search': 'Search',
        'filter': 'Filter',
        'export': 'Export',
        'import': 'Import',
        'print': 'Print',
        'download': 'Download',
        'upload': 'Upload',
        'select': 'Select',
        'from': 'From',
        'notes': 'Notes',
        'success': 'Success',
        'error': 'Error',
        'warning': 'Warning',
        'info': 'Info',
        'posted': 'Posted',
        'draft': 'Draft',
        'pending': 'Pending',
        'paid': 'Paid',
        'unpaid': 'Unpaid',
        'yes': 'Yes',
        'no': 'No',
        
        # Sales
        'create_estimate': 'Create Estimate',
        'convert_to_invoice': 'Convert to Invoice',
        'manage_invoices': 'Manage Invoices',
        'invoice': 'Invoice',
        'estimate': 'Estimate',
        'invoice_id': 'Invoice ID',
        'estimate_id': 'Estimate ID',
        'item': 'Item',
        'quantity': 'Quantity',
        'qty': 'Qty',
        'price': 'Price',
        'subtotal': 'Subtotal',
        'tax': 'Tax',
        'discount': 'Discount',
        'grand_total': 'Grand Total',
        'select_customer': 'Select Customer',
        'select_item': 'Select Item',
        'add_item': 'Add Item',
        'remove_item': 'Remove Item',
        'tax_percentage': 'Tax %',
        'discount_amount': 'Discount Amount',
        'create_invoice': 'Create Invoice',
        'save_estimate': 'Save Estimate',
        'post_invoice': 'Post Invoice',
        'convert': 'Convert',
        'invoice_details': 'Invoice Details',
        'estimate_details': 'Estimate Details',
        
        # Purchases
        'add_bill': 'Add Bill',
        'vendor_ledger': 'Vendor Ledger',
        'bill': 'Bill',
        'bill_id': 'Bill ID',
        'purchase_order': 'Purchase Order',
        'select_vendor': 'Select Vendor',
        'create_bill': 'Create Bill',
        'post_bill': 'Post Bill',
        'bill_details': 'Bill Details',
        'manage_bills': 'Manage Bills',
        
        # Inventory
        'stock_list': 'Stock List',
        'low_stock_alert': 'Low Stock Alert',
        'item_name': 'Item Name',
        'cost_price': 'Cost Price',
        'sell_price': 'Sell Price',
        'stock_quantity': 'Stock Quantity',
        'reorder_level': 'Reorder Level',
        'add_new_item': 'Add New Item',
        'update_stock': 'Update Stock',
        'current_stock': 'Current Stock',
        'stock_value': 'Stock Value',
        
        # Expenses
        'add_expense': 'Add Expense',
        'expense_list': 'Expense List',
        'category': 'Category',
        'expense': 'Expense',
        'expense_category': 'Expense Category',
        'expense_amount': 'Expense Amount',
        'expense_date': 'Expense Date',
        
        # Payments
        'customer_payment': 'Customer Payment',
        'vendor_payment': 'Vendor Payment',
        'payment': 'Payment',
        'payment_received': 'Payment Received',
        'payment_made': 'Payment Made',
        'receive_payment': 'Receive Payment',
        'make_payment': 'Make Payment',
        'payment_amount': 'Payment Amount',
        'payment_date': 'Payment Date',
        'payment_method': 'Payment Method',
        'cash': 'Cash',
        'bank': 'Bank',
        'cheque': 'Cheque',
        
        # Loans
        'loan_party': 'Loan Party',
        'loan_amount': 'Loan Amount',
        'interest_rate': 'Interest Rate',
        'loan_given': 'Loan Given',
        'loan_received': 'Loan Received',
        'add_loan': 'Add Loan',
        'loan_type': 'Loan Type',
        'party_name': 'Party Name',
        'loan_details': 'Loan Details',
        'loan_transactions': 'Loan Transactions',
        'add_transaction': 'Add Transaction',
        'transaction_type': 'Transaction Type',
        'principal': 'Principal',
        'interest': 'Interest',
        'balance': 'Balance',
        
        # Reports
        'sales_profit': 'Sales & Profit',
        'profit_loss': 'Profit & Loss',
        'customer_ledger': 'Customer Ledger',
        'vendor_ledger': 'Vendor Ledger',
        'item_wise_sales': 'Item-Wise Sales',
        'cash_flow': 'Cash Flow',
        'cash_in': 'Cash In',
        'cash_out': 'Cash Out',
        'net_cash_flow': 'Net Cash Flow',
        'select_date_range': 'Select Date Range',
        'from_date': 'From Date',
        'to_date': 'To Date',
        'generate_report': 'Generate Report',
        'export_pdf': 'Export PDF',
        'summary': 'Summary',
        'details': 'Details',
        'total_revenue': 'Total Revenue',
        'total_cost': 'Total Cost',
        'gross_profit': 'Gross Profit',
        'profit_margin': 'Profit Margin',
        'period': 'Period',
        
        # Additional Common Terms
        'name': 'Name',
        'email': 'Email',
        'phone': 'Phone',
        'address': 'Address',
        'city': 'City',
        'country': 'Country',
        'create': 'Create',
        'first': 'first',
        'new': 'New',
        'all': 'All',
        'none': 'None',
        'optional': 'Optional',
        'required': 'Required',
        'select_all': 'Select All',
        'deselect_all': 'Deselect All',
        'apply': 'Apply',
        'reset': 'Reset',
        'close': 'Close',
        'open': 'Open',
        'show': 'Show',
        'hide': 'Hide',
        'more': 'More',
        'less': 'Less',
        'back': 'Back',
        'next': 'Next',
        'previous': 'Previous',
        'finish': 'Finish',
        'confirm': 'Confirm',
        'continue': 'Continue',
        'send': 'Send',
        'receive': 'Receive',
        'history': 'History',
        'records': 'Records',
        'entries': 'Entries',
        'items': 'Items',
        'price_matrix': 'Price Matrix',
        'matrix': 'Matrix',
        'management': 'Management',
        'comprehensive_financial_analysis': 'Comprehensive Financial Analysis',
        
        # Form Fields
        'item_name': 'Item Name',
        'cost_price': 'Cost Price',
        'sell_price': 'Sell Price',
        'quantity': 'Quantity',
        'reorder_level': 'Reorder Level',
        'initial_quantity': 'Initial Quantity',
        'discount_rs': 'Discount (Rs)',
        'tax_rate_percent': 'Tax Rate (%)',
        'tax_type': 'Tax Type',
        'without_tax': 'Without Tax',
        'with_tax': 'With Tax',
        'vendor': 'Vendor',
        'number_of_items': 'Number of items',
        'select_item': 'Select Item',
        'from_date_label': '📅 From Date',
        'to_date_label': '📅 To Date',
        'status_label': '🏷️ Status',
        'draft': 'Draft',
        'posted': 'Posted',
        'paid': 'Paid',
        'search_items': '🔍 Search Items',
        'filter_by_name': 'Filter by item name...',
        'total_stock_value': '📦 Total Stock Value (Cost Price)',
        'total_customer_balance': '👥 Total Customer Balance',
        'total_vendor_balance': '🏢 Total Vendor Balance',
        'no_outstanding_customer': '✅ No outstanding customer balances',
        'no_outstanding_vendor': '✅ No outstanding vendor balances',
        'filter_by_status': 'Filter by Status',
        'pending': 'Pending',
        'approved': 'Approved',
        'completed': 'Completed',
        'cancelled': 'Cancelled',
        'select_estimate': 'Select Estimate',
        'qty': 'Qty',
        'transfer': 'Transfer',
        'check': 'Check',
        'other': 'Other',
        'memo': 'Memo',
        'amount_received_rs': 'Amount Received (Rs)',
        'amount_paid_rs': 'Amount Paid (Rs)',
        'search_placeholder': 'Filter by item name...',
        'cost': 'Cost',
        'number_of_items_label': 'Number of Items',
        'item': 'Item',
        'rejected': 'Rejected',
        'loans_given': '💸 Total Loan Given',
        'loans_received': '💸 Total Loan Received',
        
        # Login
        'sign_in': 'Sign In',
        'username': 'Username',
        'password': 'Password',
        'login_successful': 'Login successful!',
        'invalid_credentials': 'Invalid username or password',
        'enter_username': 'Enter your username',
        'enter_password': 'Enter your password',
        'business_management_system': 'Business Management System',
    },
    'العربية (Arabic)': {
        # Navigation & Main
        'dashboard': 'لوحة التحكم',
        'reports': 'التقارير',
        'sales': 'المبيعات',
        'purchases': 'المشتريات',
        'orders': 'الطلبات',
        'inventory': 'المخزون',
        'expenses': 'المصروفات',
        'loans': 'القروض',
        'payments': 'المدفوعات',
        'customers': 'العملاء',
        'vendors': 'الموردون',
        'admin': 'الإدارة',
        'backup_export': 'النسخ الاحتياطي والتصدير',
        'logout': 'تسجيل الخروج',

        # Share
        'share': 'مشاركة',
        'whatsapp': 'واتساب',
        'email': 'البريد الإلكتروني',

        # Dashboard
        'business_dashboard': 'لوحة الأعمال',
        'select_month': 'اختر الشهر',
        'language': 'اللغة',
        'for_the_period': 'للفترة',
        'to': 'إلى',
        'total_sales': 'إجمالي المبيعات',
        'cogs': 'تكلفة البضاعة المباعة',
        'total_expenses': 'إجمالي المصروفات',
        'net_profit': 'صافي الربح',
        'monthly_trend': 'الاتجاه الشهري',
        'recent_activity': 'النشاط الأخير',
        'recent_invoices': 'أحدث الفواتير',
        'recent_bills': 'أحدث فواتير الشراء',
        'recent_expenses': 'أحدث المصروفات',
        'no_data': 'لا توجد بيانات',
        'loans_given': 'قروض ممنوحة',
        'loans_received': 'قروض مستلمة',

        # Common
        'date': 'التاريخ',
        'customer': 'عميل',
        'vendor': 'مورد',
        'amount': 'المبلغ',
        'total': 'الإجمالي',
        'status': 'الحالة',
        'actions': 'الإجراءات',
        'description': 'الوصف',
        'save': 'حفظ',
        'cancel': 'إلغاء',
        'delete': 'حذف',
        'edit': 'تعديل',
        'view': 'عرض',
        'add': 'إضافة',
        'update': 'تحديث',
        'submit': 'إرسال',
        'search': 'بحث',
        'filter': 'تصفية',
        'export': 'تصدير',
        'import': 'استيراد',
        'print': 'طباعة',
        'download': 'تنزيل',
        'upload': 'رفع',
        'select': 'اختيار',
        'from': 'من',
        'notes': 'ملاحظات',
        'success': 'نجاح',
        'error': 'خطأ',
        'warning': 'تحذير',
        'info': 'معلومات',
        'posted': 'مُرحّل',
        'draft': 'مسودة',
        'pending': 'قيد الانتظار',
        'paid': 'مدفوع',
        'unpaid': 'غير مدفوع',
        'yes': 'نعم',
        'no': 'لا',

        # Sales
        'create_estimate': 'إنشاء عرض سعر',
        'convert_to_invoice': 'تحويل إلى فاتورة',
        'manage_invoices': 'إدارة الفواتير',
        'invoice': 'فاتورة',
        'estimate': 'عرض سعر',
        'invoice_id': 'رقم الفاتورة',
        'estimate_id': 'رقم عرض السعر',
        'item': 'صنف',
        'quantity': 'الكمية',
        'qty': 'الكمية',
        'price': 'السعر',
        'subtotal': 'المجموع الفرعي',
        'tax': 'ضريبة',
        'discount': 'خصم',
        'grand_total': 'الإجمالي',
        'select_customer': 'اختر العميل',
        'select_item': 'اختر الصنف',
        'add_item': 'إضافة صنف',
        'remove_item': 'إزالة صنف',
        'tax_percentage': 'نسبة الضريبة',
        'discount_amount': 'مبلغ الخصم',
        'create_invoice': 'إنشاء فاتورة',
        'save_estimate': 'حفظ عرض السعر',
        'post_invoice': 'ترحيل الفاتورة',
        'convert': 'تحويل',
        'invoice_details': 'تفاصيل الفاتورة',
        'estimate_details': 'تفاصيل عرض السعر',

        # Purchases
        'add_bill': 'إضافة فاتورة شراء',
        'vendor_ledger': 'دفتر الموردين',
        'bill': 'فاتورة شراء',
        'bill_id': 'رقم فاتورة الشراء',
        'purchase_order': 'أمر شراء',
        'select_vendor': 'اختر المورد',
        'create_bill': 'إنشاء فاتورة شراء',
        'post_bill': 'ترحيل فاتورة الشراء',
        'bill_details': 'تفاصيل فاتورة الشراء',
        'manage_bills': 'إدارة فواتير الشراء',

        # Inventory
        'stock_list': 'قائمة المخزون',
        'low_stock_alert': 'تنبيه انخفاض المخزون',
        'item_name': 'اسم الصنف',
        'cost_price': 'سعر التكلفة',
        'sell_price': 'سعر البيع',
        'stock_quantity': 'كمية المخزون',
        'reorder_level': 'حد إعادة الطلب',
        'add_new_item': 'إضافة صنف جديد',
        'update_stock': 'تحديث المخزون',
        'current_stock': 'المخزون الحالي',
        'stock_value': 'قيمة المخزون',

        # Expenses
        'add_expense': 'إضافة مصروف',
        'expense_list': 'قائمة المصروفات',
        'category': 'الفئة',
        'expense': 'مصروف',
        'expense_category': 'فئة المصروف',
        'expense_amount': 'مبلغ المصروف',
        'expense_date': 'تاريخ المصروف',

        # Payments
        'customer_payment': 'دفعة عميل',
        'vendor_payment': 'دفعة مورد',
        'payment': 'دفعة',
        'payment_received': 'دفعة مستلمة',
        'payment_made': 'دفعة مدفوعة',
        'receive_payment': 'استلام دفعة',
        'make_payment': 'دفع مبلغ',
        'payment_amount': 'مبلغ الدفعة',
        'payment_date': 'تاريخ الدفعة',
        'payment_method': 'طريقة الدفع',
        'cash': 'نقداً',
        'bank': 'بنك',
        'cheque': 'شيك',

        # Loans
        'loan_party': 'جهة القرض',
        'loan_amount': 'مبلغ القرض',
        'interest_rate': 'معدل الفائدة',
        'loan_given': 'قرض ممنوح',
        'loan_received': 'قرض مستلم',
        'add_loan': 'إضافة قرض',
        'loan_type': 'نوع القرض',
        'party_name': 'اسم الجهة',
        'loan_details': 'تفاصيل القرض',
        'loan_transactions': 'معاملات القرض',
        'add_transaction': 'إضافة معاملة',
        'transaction_type': 'نوع المعاملة',
        'principal': 'أصل المبلغ',
        'interest': 'فائدة',
        'balance': 'الرصيد',

        # Reports
        'sales_profit': 'المبيعات والربح',
        'profit_loss': 'الأرباح والخسائر',
        'customer_ledger': 'دفتر العملاء',
        'vendor_ledger': 'دفتر الموردين',
        'item_wise_sales': 'المبيعات حسب الصنف',
        'cash_flow': 'التدفق النقدي',
        'cash_in': 'نقد داخل',
        'cash_out': 'نقد خارج',
        'net_cash_flow': 'صافي التدفق النقدي',
        'select_date_range': 'اختر نطاق التاريخ',
        'from_date': 'من تاريخ',
        'to_date': 'إلى تاريخ',
        'generate_report': 'إنشاء تقرير',
        'export_pdf': 'تصدير PDF',
        'summary': 'ملخص',
        'details': 'تفاصيل',
        'total_revenue': 'إجمالي الإيرادات',
        'total_cost': 'إجمالي التكلفة',
        'gross_profit': 'إجمالي الربح',
        'profit_margin': 'هامش الربح',
        'period': 'الفترة',

        # Additional Common Terms
        'name': 'الاسم',
        'email': 'البريد الإلكتروني',
        'phone': 'الهاتف',
        'address': 'العنوان',
        'city': 'المدينة',
        'country': 'الدولة',
        'create': 'إنشاء',
        'first': 'أولاً',
        'new': 'جديد',
        'all': 'الكل',
        'none': 'لا شيء',
        'optional': 'اختياري',
        'required': 'مطلوب',
        'select_all': 'تحديد الكل',
        'deselect_all': 'إلغاء تحديد الكل',
        'apply': 'تطبيق',
        'reset': 'إعادة ضبط',
        'close': 'إغلاق',
        'open': 'فتح',
        'show': 'عرض',
        'hide': 'إخفاء',
        'more': 'المزيد',
        'less': 'أقل',
        'back': 'رجوع',
        'next': 'التالي',
        'previous': 'السابق',
        'finish': 'إنهاء',
        'confirm': 'تأكيد',
        'continue': 'متابعة',
        'send': 'إرسال',
        'receive': 'استلام',
        'history': 'السجل',
        'records': 'السجلات',
        'entries': 'القيود',
        'items': 'العناصر',
        'price_matrix': 'مصفوفة الأسعار',
        'matrix': 'مصفوفة',
        'management': 'الإدارة',
        'comprehensive_financial_analysis': 'تحليل مالي شامل',

        # Form Fields
        'initial_quantity': 'الكمية الابتدائية',
        'discount_rs': 'الخصم (روبية)',
        'tax_rate_percent': 'معدل الضريبة (%)',
        'tax_type': 'نوع الضريبة',
        'without_tax': 'بدون ضريبة',
        'with_tax': 'مع ضريبة',
        'number_of_items': 'عدد الأصناف',
        'from_date_label': '📅 من تاريخ',
        'to_date_label': '📅 إلى تاريخ',
        'status_label': '🏷️ الحالة',
        'search_items': '🔍 بحث عن الأصناف',
        'filter_by_name': 'تصفية حسب اسم الصنف...',
        'total_stock_value': '📦 إجمالي قيمة المخزون (سعر التكلفة)',
        'total_customer_balance': '👥 إجمالي رصيد العملاء',
        'total_vendor_balance': '🏢 إجمالي رصيد الموردين',
        'no_outstanding_customer': '✅ لا توجد أرصدة مستحقة على العملاء',
        'no_outstanding_vendor': '✅ لا توجد أرصدة مستحقة على الموردين',
        'filter_by_status': 'تصفية حسب الحالة',
        'approved': 'معتمد',
        'completed': 'مكتمل',
        'cancelled': 'ملغي',
        'select_estimate': 'اختر عرض السعر',
        'transfer': 'تحويل',
        'check': 'شيك',
        'other': 'أخرى',
        'memo': 'مذكرة',
        'amount_received_rs': 'المبلغ المستلم (روبية)',
        'amount_paid_rs': 'المبلغ المدفوع (روبية)',
        'search_placeholder': 'تصفية حسب اسم الصنف...',
        'cost': 'التكلفة',
        'number_of_items_label': 'عدد الأصناف',
        'rejected': 'مرفوض',
        'loans_given': '💸 إجمالي القروض الممنوحة',
        'loans_received': '💸 إجمالي القروض المستلمة',

        # Login
        'sign_in': 'تسجيل الدخول',
        'username': 'اسم المستخدم',
        'password': 'كلمة المرور',
        'login_successful': 'تم تسجيل الدخول بنجاح!',
        'invalid_credentials': 'اسم مستخدم أو كلمة مرور غير صحيحة',
        'enter_username': 'أدخل اسم المستخدم',
        'enter_password': 'أدخل كلمة المرور',
        'business_management_system': 'نظام إدارة الأعمال',
    },
    'اردو (Urdu)': {
        # Navigation & Main
        'dashboard': 'ڈیش بورڈ',
        'reports': 'رپورٹس',
        'sales': 'فروخت',
        'purchases': 'خریداری',
        'orders': 'آرڈرز',
        'inventory': 'انوینٹری',
        'expenses': 'اخراجات',
        'loans': 'قرض',
        'payments': 'ادائیگی',
        'customers': 'گاہک',
        'vendors': 'فروخت کنندگان',
        'admin': 'ایڈمن',
        'backup_export': 'بیک اپ اور ایکسپورٹ',
        'logout': 'لاگ آؤٹ',

        # Share
        'share': 'شیئر',
        'whatsapp': 'واٹس ایپ',
        'email': 'ای میل',
        
        # Dashboard
        'business_dashboard': 'کاروباری ڈیش بورڈ',
        'select_month': 'مہینہ منتخب کریں',
        'language': 'زبان',
        'for_the_period': 'مدت کے لیے',
        'to': 'تک',
        'total_sales': 'کل فروخت',
        'cogs': 'مال کی لاگت',
        'total_expenses': 'کل اخراجات',
        'net_profit': 'خالص منافع',
        'monthly_trend': 'ماہانہ رجحان',
        'recent_activity': 'حالیہ سرگرمی',
        'recent_invoices': 'حالیہ انوائسز',
        'recent_bills': 'حالیہ بلز',
        'recent_expenses': 'حالیہ اخراجات',
        'no_data': 'کوئی ڈیٹا دستیاب نہیں',
        'loans_given': 'دیے گئے قرض',
        'loans_received': 'لیے گئے قرض',
        
        # Common
        'date': 'تاریخ',
        'customer': 'گاہک',
        'vendor': 'فروخت کنندہ',
        'amount': 'رقم',
        'total': 'کل',
        'status': 'حالت',
        'actions': 'اقدامات',
        'description': 'تفصیل',
        'save': 'محفوظ کریں',
        'cancel': 'منسوخ کریں',
        'delete': 'حذف کریں',
        'edit': 'ترمیم کریں',
        'view': 'دیکھیں',
        'add': 'شامل کریں',
        'update': 'اپ ڈیٹ کریں',
        'submit': 'جمع کرائیں',
        'search': 'تلاش کریں',
        'filter': 'فلٹر کریں',
        'export': 'ایکسپورٹ کریں',
        'import': 'امپورٹ کریں',
        'print': 'پرنٹ کریں',
        'download': 'ڈاؤن لوڈ کریں',
        'upload': 'اپ لوڈ کریں',
        'select': 'منتخب کریں',
        'from': 'سے',
        'notes': 'نوٹس',
        'success': 'کامیابی',
        'error': 'خرابی',
        'warning': 'انتباہ',
        'info': 'معلومات',
        'posted': 'پوسٹ شدہ',
        'draft': 'ڈرافٹ',
        'pending': 'زیر التواء',
        'paid': 'ادا شدہ',
        'unpaid': 'غیر ادا شدہ',
        'yes': 'ہاں',
        'no': 'نہیں',
        
        # Sales
        'create_estimate': 'تخمینہ بنائیں',
        'convert_to_invoice': 'انوائس میں تبدیل کریں',
        'manage_invoices': 'انوائسز منظم کریں',
        'invoice': 'انوائس',
        'estimate': 'تخمینہ',
        'invoice_id': 'انوائس نمبر',
        'estimate_id': 'تخمینہ نمبر',
        'item': 'آئٹم',
        'quantity': 'مقدار',
        'qty': 'تعداد',
        'price': 'قیمت',
        'subtotal': 'ذیلی کل',
        'tax': 'ٹیکس',
        'discount': 'رعایت',
        'grand_total': 'کل رقم',
        'select_customer': 'گاہک منتخب کریں',
        'select_item': 'آئٹم منتخب کریں',
        'add_item': 'آئٹم شامل کریں',
        'remove_item': 'آئٹم ہٹائیں',
        'tax_percentage': 'ٹیکس فیصد',
        'discount_amount': 'رعایت کی رقم',
        'create_invoice': 'انوائس بنائیں',
        'save_estimate': 'تخمینہ محفوظ کریں',
        'post_invoice': 'انوائس پوسٹ کریں',
        'convert': 'تبدیل کریں',
        'invoice_details': 'انوائس کی تفصیلات',
        'estimate_details': 'تخمینہ کی تفصیلات',
        
        # Purchases
        'add_bill': 'بل شامل کریں',
        'vendor_ledger': 'فروخت کنندہ لیجر',
        'bill': 'بل',
        'bill_id': 'بل نمبر',
        'purchase_order': 'خریداری آرڈر',
        'select_vendor': 'فروخت کنندہ منتخب کریں',
        'create_bill': 'بل بنائیں',
        'post_bill': 'بل پوسٹ کریں',
        'bill_details': 'بل کی تفصیلات',
        'manage_bills': 'بلز منظم کریں',
        
        # Inventory
        'stock_list': 'سٹاک کی فہرست',
        'low_stock_alert': 'کم سٹاک الرٹ',
        'item_name': 'آئٹم کا نام',
        'cost_price': 'لاگت کی قیمت',
        'sell_price': 'فروخت کی قیمت',
        'stock_quantity': 'سٹاک کی مقدار',
        'reorder_level': 'دوبارہ آرڈر کی سطح',
        'add_new_item': 'نیا آئٹم شامل کریں',
        'update_stock': 'سٹاک اپ ڈیٹ کریں',
        'current_stock': 'موجودہ سٹاک',
        'stock_value': 'سٹاک کی قیمت',
        
        # Expenses
        'add_expense': 'اخراجات شامل کریں',
        'expense_list': 'اخراجات کی فہرست',
        'category': 'زمرہ',
        'expense': 'اخراجات',
        'expense_category': 'اخراجات کا زمرہ',
        'expense_amount': 'اخراجات کی رقم',
        'expense_date': 'اخراجات کی تاریخ',
        
        # Payments
        'customer_payment': 'گاہک کی ادائیگی',
        'vendor_payment': 'فروخت کنندہ کی ادائیگی',
        'payment': 'ادائیگی',
        'payment_received': 'ادائیگی وصول',
        'payment_made': 'ادائیگی کی گئی',
        'receive_payment': 'ادائیگی وصول کریں',
        'make_payment': 'ادائیگی کریں',
        'payment_amount': 'ادائیگی کی رقم',
        'payment_date': 'ادائیگی کی تاریخ',
        'payment_method': 'ادائیگی کا طریقہ',
        'cash': 'نقد',
        'bank': 'بینک',
        'cheque': 'چیک',
        
        # Loans
        'loan_party': 'قرض لینے والا',
        'loan_amount': 'قرض کی رقم',
        'interest_rate': 'سود کی شرح',
        'loan_given': 'قرض دیا',
        'loan_received': 'قرض لیا',
        'add_loan': 'قرض شامل کریں',
        'loan_type': 'قرض کی قسم',
        'party_name': 'پارٹی کا نام',
        'loan_details': 'قرض کی تفصیلات',
        'loan_transactions': 'قرض کے لین دین',
        'add_transaction': 'لین دین شامل کریں',
        'transaction_type': 'لین دین کی قسم',
        'principal': 'اصل رقم',
        'interest': 'سود',
        'balance': 'بقایا',
        
        # Reports
        'sales_profit': 'فروخت اور منافع',
        'profit_loss': 'منافع اور نقصان',
        'customer_ledger': 'گاہک لیجر',
        'vendor_ledger': 'فروخت کنندہ لیجر',
        'item_wise_sales': 'آئٹم کے لحاظ سے فروخت',
        'cash_flow': 'کیش فلو',
        'cash_in': 'موصولہ نقد',
        'cash_out': 'ادا شدہ نقد',
        'net_cash_flow': 'خالص کیش فلو',
        'select_date_range': 'تاریخ کی حد منتخب کریں',
        'from_date': 'تاریخ سے',
        'to_date': 'تاریخ تک',
        'generate_report': 'رپورٹ بنائیں',
        'export_pdf': 'پی ڈی ایف ایکسپورٹ کریں',
        'summary': 'خلاصہ',
        'details': 'تفصیلات',
        'total_revenue': 'کل آمدنی',
        'total_cost': 'کل لاگت',
        'gross_profit': 'مجموعی منافع',
        'profit_margin': 'منافع کا مارجن',
        'period': 'مدت',
        
        # Additional Common Terms
        'name': 'نام',
        'email': 'ای میل',
        'phone': 'فون',
        'address': 'پتہ',
        'city': 'شہر',
        'country': 'ملک',
        'create': 'بنائیں',
        'first': 'پہلے',
        'new': 'نیا',
        'all': 'تمام',
        'none': 'کوئی نہیں',
        'optional': 'اختیاری',
        'required': 'ضروری',
        'select_all': 'سب منتخب کریں',
        'deselect_all': 'منتخب ختم کریں',
        'apply': 'لاگو کریں',
        'reset': 'ری سیٹ کریں',
        'close': 'بند کریں',
        'open': 'کھولیں',
        'show': 'دکھائیں',
        'hide': 'چھپائیں',
        'more': 'مزید',
        'less': 'کم',
        'back': 'واپس',
        'next': 'اگلا',
        'previous': 'پچھلا',
        'finish': 'ختم کریں',
        'confirm': 'تصدیق کریں',
        'continue': 'جاری رکھیں',
        'send': 'بھیجیں',
        'receive': 'وصول کریں',
        'history': 'تاریخ',
        'records': 'ریکارڈ',
        'entries': 'اندراجات',
        'items': 'آئٹمز',
        'price_matrix': 'قیمت میٹرکس',
        'matrix': 'میٹرکس',
        'management': 'انتظام',
        'comprehensive_financial_analysis': 'جامع مالی تجزیہ',
        
        # Form Fields
        'item_name': 'آئٹم کا نام',
        'cost_price': 'قیمت خرید',
        'sell_price': 'قیمت فروخت',
        'quantity': 'مقدار',
        'reorder_level': 'دوبارہ آرڈر کی سطح',
        'initial_quantity': 'ابتدائی مقدار',
        'discount_rs': 'رعایت (روپے)',
        'tax_rate_percent': 'ٹیکس کی شرح (%)',
        'tax_type': 'ٹیکس کی قسم',
        'without_tax': 'بغیر ٹیکس',
        'with_tax': 'ٹیکس کے ساتھ',
        'vendor': 'فروخت کنندہ',
        'number_of_items': 'اشیاء کی تعداد',
        'select_item': 'آئٹم منتخب کریں',
        'from_date_label': '📅 تاریخ سے',
        'to_date_label': '📅 تاریخ تک',
        'status_label': '🏷️ حالت',
        'draft': 'مسودہ',
        'posted': 'پوسٹ شدہ',
        'paid': 'ادا شدہ',
        'search_items': '🔍 اشیاء تلاش کریں',
        'filter_by_name': 'آئٹم کے نام سے فلٹر کریں...',
        'total_stock_value': '📦 کل اسٹاک کی قیمت (قیمت خرید)',
        'total_customer_balance': '👥 کل گاہک کی بقایا',
        'total_vendor_balance': '🏢 کل فروخت کنندہ کی بقایا',
        'no_outstanding_customer': '✅ کوئی بقایا گاہک کی رقم نہیں',
        'no_outstanding_vendor': '✅ کوئی بقایا فروخت کنندہ کی رقم نہیں',
        'filter_by_status': 'حالت کے لحاظ سے فلٹر کریں',
        'pending': 'زیر التواء',
        'approved': 'منظور شدہ',
        'completed': 'مکمل',
        'cancelled': 'منسوخ',
        'select_estimate': 'تخمینہ منتخب کریں',
        'qty': 'مقدار',
        'transfer': 'منتقلی',
        'check': 'چیک',
        'other': 'دیگر',
        'memo': 'یاد داشت',
        'amount_received_rs': 'وصول شدہ رقم (روپے)',
        'amount_paid_rs': 'ادا شدہ رقم (روپے)',
        'search_placeholder': 'آئٹم کے نام سے فلٹر کریں...',
        'cost': 'لاگت',
        'number_of_items_label': 'اشیاء کی تعداد',
        'item': 'آئٹم',
        'rejected': 'مسترد',
        'loans_given': '💸 کل قرض دیا',
        'loans_received': '💸 کل قرض لیا',
        
        'interest_rate': 'سود کی شرح',
        'loan_given': 'قرض دیا',
        'loan_received': 'قرض لیا',
        'add_loan': 'قرض شامل کریں',
        'loan_type': 'قرض کی قسم',
        'party_name': 'پارٹی کا نام',
        'loan_details': 'قرض کی تفصیلات',
        'loan_transactions': 'قرض کے لین دین',
        'add_transaction': 'لین دین شامل کریں',
        'transaction_type': 'لین دین کی قسم',
        'principal': 'اصل رقم',
        'interest': 'سود',
        'balance': 'بقایا',
        
        # Reports
        'sales_profit': 'فروخت اور منافع',
        'profit_loss': 'منافع اور نقصان',
        'customer_ledger': 'گاہک لیجر',
        'vendor_ledger': 'فروخت کنندہ لیجر',
        'item_wise_sales': 'آئٹم کے لحاظ سے فروخت',
        'select_date_range': 'تاریخ کی حد منتخب کریں',
        'from_date': 'تاریخ سے',
        'to_date': 'تاریخ تک',
        'generate_report': 'رپورٹ بنائیں',
        'export_pdf': 'پی ڈی ایف ایکسپورٹ کریں',
        'summary': 'خلاصہ',
        'details': 'تفصیلات',
        'total_revenue': 'کل آمدنی',
        'total_cost': 'کل لاگت',
        'gross_profit': 'مجموعی منافع',
        'profit_margin': 'منافع کا مارجن',
        'period': 'مدت',
        
        # Login
        'sign_in': 'سائن ان کریں',
        'username': 'صارف نام',
        'password': 'پاس ورڈ',
        'login_successful': 'لاگ ان کامیاب!',
        'invalid_credentials': 'غلط صارف نام یا پاس ورڈ',
        'enter_username': 'اپنا صارف نام درج کریں',
        'enter_password': 'اپنا پاس ورڈ درج کریں',
        'business_management_system': 'کاروباری انتظام کا نظام',
    }
}

def t(key, lang=None):
    """Translate a key to the current language."""
    if lang is None:
        lang = st.session_state.get('language', 'English')
    return TRANSLATIONS.get(lang, TRANSLATIONS['English']).get(key, key)

# ==================== HELPERS ====================
def hash_password(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), PASSWORD_SALT, PASSWORD_ITERATIONS).hex()

def verify_password(password: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password), expected_hash)

logging.basicConfig(level=logging.INFO)


def to_decimal_safe(v, default=Decimal("0.00")):
    """Safely convert various inputs to Decimal.

    Strips common currency symbols, commas and whitespace before conversion.
    Always returns a Decimal.
    """
    if v is None or v == "":
        return default
    if isinstance(v, Decimal):
        return v
    try:
        s = str(v).strip()
        # Remove currency symbols, commas, spaces; keep minus and dot
        s = re.sub(r"[^0-9.\-]", "", s)
        if s == "":
            return default
        return Decimal(s)
    except Exception:
        logging.exception("to_decimal_safe: failed to parse %r", v)
        return default


def normalize_items_list(items):
    """Ensure each item dict has consistent keys: item_id, item (name), qty, price, total."""
    if not items:
        return []

    inventory = load_table("inventory")
    normalized = []
    for it in items:
        try:
            # accept various key names
            item_name = it.get('item') or it.get('Item') or it.get('ItemName') or ''
            item_id = it.get('item_id') or it.get('ItemID')
            qty = it.get('qty') if it.get('qty') is not None else it.get('quantity') if it.get('quantity') is not None else it.get('Qty', 0)
            price = it.get('price') if it.get('price') is not None else it.get('Price') if it.get('Price') is not None else 0
            total = it.get('total') if it.get('total') is not None else it.get('Total') if it.get('Total') is not None else None

            # Resolve item_id from name if missing
            if not item_id and item_name:
                match = inventory[inventory['ItemName'] == item_name]
                if not match.empty:
                    item_id = match.iloc[0]['ItemID']

            # Coerce numeric types
            qty = int(qty) if qty is not None and str(qty) != '' else 0
            price = to_decimal_safe(price, Decimal('0.00'))
            if total is None:
                total = price * Decimal(qty)
            else:
                total = to_decimal_safe(total, Decimal('0.00'))

            # Get display name from inventory if missing
            if not item_name and item_id:
                match = inventory[inventory['ItemID'] == item_id]
                if not match.empty:
                    item_name = match.iloc[0]['ItemName']

            normalized.append({
                'item_id': item_id,
                'item': item_name,
                'qty': qty,
                'price': str(to_decimal_safe(price)),
                'total': str(to_decimal_safe(total))
            })
        except Exception:
            logging.exception("normalize_items_list: failed for item %s", it)
    return normalized


def parse_items_json(items_json):
    """Safely parse ItemsJSON (string or list) and return normalized list of items."""
    try:
        items = json.loads(items_json) if isinstance(items_json, str) else items_json
    except Exception:
        try:
            items = json.loads(json.loads(items_json))
        except Exception:
            logging.exception("parse_items_json: failed to parse ItemsJSON")
            return []
    if isinstance(items, dict):
        items = list(items.values())
    return normalize_items_list(items)


def format_currency(v):
    try:
        return f"Rs {to_decimal_safe(v):,.2f}"
    except Exception:
        return f"Rs 0.00"

# ==================== DATABASE ====================
@contextmanager
def _get_db_for_path(db_path: Path):
    """Get database connection for a specific SQLite file."""
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()


@contextmanager
def get_db():
    """Get database connection with proper settings."""
    with _get_db_for_path(get_active_db_path()) as conn:
        yield conn

def init_db(db_path: Optional[Path] = None):
    """Initialize database with all tables."""
    db_path = db_path or get_active_db_path()
    with _get_db_for_path(db_path) as conn:
        # Inventory
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                ItemID TEXT PRIMARY KEY,
                ItemName TEXT NOT NULL UNIQUE,
                Quantity INTEGER DEFAULT 0 CHECK(Quantity >= 0),
                CostPrice DECIMAL(15,2) DEFAULT 0.00 CHECK(CostPrice >= 0),
                SellPrice DECIMAL(15,2) DEFAULT 0.00 CHECK(SellPrice >= 0),
                ReorderLevel INTEGER DEFAULT 10 CHECK(ReorderLevel >= 0),
                Active TEXT DEFAULT 'Yes'
            )
        """)
        
        # Customers
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                CustomerID TEXT PRIMARY KEY,
                CustomerName TEXT NOT NULL UNIQUE,
                Email TEXT,
                Phone TEXT,
                Address TEXT,
                OpenBalance DECIMAL(15,2) DEFAULT 0.00,
                Active TEXT DEFAULT 'Yes'
            )
        """)
        
        # Vendors
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendors (
                VendorID TEXT PRIMARY KEY,
                VendorName TEXT NOT NULL UNIQUE,
                Email TEXT,
                Phone TEXT,
                Address TEXT,
                OpenBalance DECIMAL(15,2) DEFAULT 0.00,
                Active TEXT DEFAULT 'Yes'
            )
        """)
        
        # Customer Item Prices (override default sell price)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_item_prices (
                PriceID TEXT PRIMARY KEY,
                CustomerID TEXT NOT NULL,
                ItemID TEXT NOT NULL,
                CustomPrice DECIMAL(15,2) NOT NULL CHECK(CustomPrice >= 0),
                FOREIGN KEY(CustomerID) REFERENCES customers(CustomerID),
                FOREIGN KEY(ItemID) REFERENCES inventory(ItemID),
                UNIQUE(CustomerID, ItemID)
            )
        """)

        # Vendor Item Prices (override default cost price when creating bills)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendor_item_prices (
                PriceID TEXT PRIMARY KEY,
                VendorID TEXT NOT NULL,
                ItemID TEXT NOT NULL,
                LockedPrice DECIMAL(15,2) NOT NULL CHECK(LockedPrice >= 0),
                FOREIGN KEY(VendorID) REFERENCES vendors(VendorID),
                FOREIGN KEY(ItemID) REFERENCES inventory(ItemID),
                UNIQUE(VendorID, ItemID)
            )
        """)
        
        # Invoice Line Items (relational alternative to ItemsJSON)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoice_items (
                LineID TEXT PRIMARY KEY,
                InvoiceID TEXT NOT NULL,
                ItemID TEXT,
                ItemName TEXT NOT NULL,
                Quantity INTEGER NOT NULL CHECK(Quantity > 0),
                Price DECIMAL(15,2) NOT NULL CHECK(Price >= 0),
                Total DECIMAL(15,2) NOT NULL CHECK(Total >= 0),
                FOREIGN KEY(InvoiceID) REFERENCES invoices(InvoiceID) ON DELETE CASCADE,
                FOREIGN KEY(ItemID) REFERENCES inventory(ItemID)
            )
        """)
        
        # Bill Line Items (relational alternative to ItemsJSON)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bill_items (
                LineID TEXT PRIMARY KEY,
                BillID TEXT NOT NULL,
                ItemID TEXT,
                ItemName TEXT NOT NULL,
                Quantity INTEGER NOT NULL CHECK(Quantity > 0),
                Price DECIMAL(15,2) NOT NULL CHECK(Price >= 0),
                Total DECIMAL(15,2) NOT NULL CHECK(Total >= 0),
                FOREIGN KEY(BillID) REFERENCES bills(BillID) ON DELETE CASCADE,
                FOREIGN KEY(ItemID) REFERENCES inventory(ItemID)
            )
        """)
        
        # Estimate Line Items (relational alternative to ItemsJSON)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS estimate_items (
                LineID TEXT PRIMARY KEY,
                EstimateID TEXT NOT NULL,
                ItemID TEXT,
                ItemName TEXT NOT NULL,
                Quantity INTEGER NOT NULL CHECK(Quantity > 0),
                Price DECIMAL(15,2) NOT NULL CHECK(Price >= 0),
                Total DECIMAL(15,2) NOT NULL CHECK(Total >= 0),
                FOREIGN KEY(EstimateID) REFERENCES estimates(EstimateID) ON DELETE CASCADE,
                FOREIGN KEY(ItemID) REFERENCES inventory(ItemID)
            )
        """)
        
        # Estimates
        conn.execute("""
            CREATE TABLE IF NOT EXISTS estimates (
                EstimateID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                CustomerID TEXT NOT NULL,
                ItemsJSON TEXT NOT NULL,
                Subtotal DECIMAL(15,2) DEFAULT 0.00,
                TaxType TEXT DEFAULT 'Without Tax',
                TaxRate DECIMAL(5,2) DEFAULT 0.00,
                Tax DECIMAL(15,2) DEFAULT 0.00,
                Discount DECIMAL(15,2) DEFAULT 0.00,
                Total DECIMAL(15,2) DEFAULT 0.00,
                Status TEXT DEFAULT 'Draft',
                Notes TEXT,
                FOREIGN KEY(CustomerID) REFERENCES customers(CustomerID)
            )
        """)
        
        # Invoices
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                InvoiceID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                CustomerID TEXT NOT NULL,
                EstimateID TEXT,
                ItemsJSON TEXT NOT NULL,
                Subtotal DECIMAL(15,2) DEFAULT 0.00,
                TaxType TEXT DEFAULT 'Without Tax',
                TaxRate DECIMAL(5,2) DEFAULT 0.00,
                Tax DECIMAL(15,2) DEFAULT 0.00,
                Discount DECIMAL(15,2) DEFAULT 0.00,
                Total DECIMAL(15,2) DEFAULT 0.00,
                Status TEXT DEFAULT 'Draft',
                Notes TEXT,
                FOREIGN KEY(CustomerID) REFERENCES customers(CustomerID),
                FOREIGN KEY(EstimateID) REFERENCES estimates(EstimateID)
            )
        """)
        
        # Bills
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bills (
                BillID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                VendorID TEXT NOT NULL,
                ItemsJSON TEXT NOT NULL,
                Subtotal DECIMAL(15,2) DEFAULT 0.00,
                TaxType TEXT DEFAULT 'Without Tax',
                TaxRate DECIMAL(5,2) DEFAULT 0.00,
                Tax DECIMAL(15,2) DEFAULT 0.00,
                Discount DECIMAL(15,2) DEFAULT 0.00,
                Total DECIMAL(15,2) DEFAULT 0.00,
                Status TEXT DEFAULT 'Draft',
                Notes TEXT,
                FOREIGN KEY(VendorID) REFERENCES vendors(VendorID)
            )
        """)
        
        # Purchase Orders
        conn.execute("""
            CREATE TABLE IF NOT EXISTS purchase_orders (
                POID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                VendorID TEXT NOT NULL,
                ItemsJSON TEXT NOT NULL,
                Subtotal DECIMAL(15,2) DEFAULT 0.00,
                TaxType TEXT DEFAULT 'Without Tax',
                TaxRate DECIMAL(5,2) DEFAULT 0.00,
                Tax DECIMAL(15,2) DEFAULT 0.00,
                Discount DECIMAL(15,2) DEFAULT 0.00,
                Total DECIMAL(15,2) DEFAULT 0.00,
                Status TEXT DEFAULT 'Pending',
                Notes TEXT,
                FOREIGN KEY(VendorID) REFERENCES vendors(VendorID)
            )
        """)
        
        # Expenses
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                ExpenseID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                Category TEXT NOT NULL,
                Description TEXT,
                Amount DECIMAL(15,2) NOT NULL,
                Status TEXT DEFAULT 'Recorded'
            )
        """)
        
        # Users
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                Username TEXT PRIMARY KEY,
                PasswordHash TEXT NOT NULL,
                Role TEXT DEFAULT 'user',
                IsActive TEXT DEFAULT 'Yes'
            )
        """)
        
        # Backup Configuration
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backup_config (
                ConfigKey TEXT PRIMARY KEY,
                ConfigValue TEXT NOT NULL
            )
        """)
        
        # Customer Payments
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_payments (
                PaymentID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                CustomerID TEXT NOT NULL,
                Amount DECIMAL(15,2) NOT NULL,
                PaymentMethod TEXT DEFAULT 'Cash',
                Notes TEXT,
                FOREIGN KEY(CustomerID) REFERENCES customers(CustomerID)
            )
        """)
        
        # Vendor Payments
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendor_payments (
                PaymentID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                VendorID TEXT NOT NULL,
                Amount DECIMAL(15,2) NOT NULL,
                PaymentMethod TEXT DEFAULT 'Cash',
                Notes TEXT,
                FOREIGN KEY(VendorID) REFERENCES vendors(VendorID)
            )
        """)

        # Loan Parties (people we take/give loans to) and their transactions
        conn.execute("""
            CREATE TABLE IF NOT EXISTS loan_parties (
                PartyID TEXT PRIMARY KEY,
                PartyName TEXT NOT NULL UNIQUE,
                Email TEXT,
                Phone TEXT,
                Address TEXT,
                Balance DECIMAL(15,2) DEFAULT 0.00,
                Active TEXT DEFAULT 'Yes'
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS loan_transactions (
                TransactionID TEXT PRIMARY KEY,
                Date TEXT NOT NULL,
                PartyID TEXT NOT NULL,
                Amount DECIMAL(15,2) NOT NULL,
                Direction TEXT NOT NULL,
                Notes TEXT,
                FOREIGN KEY(PartyID) REFERENCES loan_parties(PartyID)
            )
        """)

        # Orders (salesman orders / item shortage requests)
        init_orders_tables(conn)
        
        # Initialize backup config defaults
        existing_config = conn.execute("SELECT ConfigKey FROM backup_config WHERE ConfigKey = 'auto_backup_enabled'").fetchone()
        if not existing_config:
            conn.execute("INSERT INTO backup_config (ConfigKey, ConfigValue) VALUES (?, ?)", ("auto_backup_enabled", "No"))
            conn.execute("INSERT INTO backup_config (ConfigKey, ConfigValue) VALUES (?, ?)", ("last_backup_date", ""))
        
        # Seed default admin
        existing = conn.execute("SELECT Username FROM users WHERE Username = 'admin'").fetchone()
        if not existing:
            admin_hash = hash_password(DEFAULT_ADMIN_PASSWORD)
            conn.execute(
                "INSERT INTO users (Username, PasswordHash, Role, IsActive) VALUES (?, ?, ?, ?)",
                ("admin", admin_hash, "admin", "Yes")
            )
        
        # Add OpenBalance column to customers if not exists (backward compatibility)
        try:
            conn.execute("ALTER TABLE customers ADD COLUMN OpenBalance DECIMAL(15,2) DEFAULT 0.00")
        except sqlite3.OperationalError:
            # Column already exists
            logging.debug("customers.OpenBalance column exists")
        except Exception:
            logging.exception("Unexpected error adding OpenBalance to customers")
        
        # Add OpenBalance column to vendors if not exists (backward compatibility)
        try:
            conn.execute("ALTER TABLE vendors ADD COLUMN OpenBalance DECIMAL(15,2) DEFAULT 0.00")
        except sqlite3.OperationalError:
            # Column already exists
            logging.debug("vendors.OpenBalance column exists")
        except Exception:
            logging.exception("Unexpected error adding OpenBalance to vendors")
        
        # Create indexes for frequently filtered columns to improve performance
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(CustomerID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(Date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(Status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bills_vendor ON bills(VendorID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bills_date ON bills(Date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(Status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_estimates_customer ON estimates(CustomerID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_payments_customer ON customer_payments(CustomerID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vendor_payments_vendor ON vendor_payments(VendorID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_loan_transactions_party ON loan_transactions(PartyID)")
            
            # Add indexes on line item tables for efficient queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(InvoiceID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_items_item ON invoice_items(ItemID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(BillID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bill_items_item ON bill_items(ItemID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_estimate_items_estimate ON estimate_items(EstimateID)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_estimate_items_item ON estimate_items(ItemID)")
            
            logging.info("Database indexes created successfully")
        except Exception:
            logging.exception("Error creating database indexes")
        
        # Create trigger to validate stock availability before invoice posting
        # This provides database-level enforcement of inventory constraints
        try:
            conn.execute("DROP TRIGGER IF EXISTS validate_stock_before_invoice_post")
            conn.execute("""
                CREATE TRIGGER validate_stock_before_invoice_post
                BEFORE UPDATE OF Status ON invoices
                FOR EACH ROW
                WHEN NEW.Status = 'Posted' AND OLD.Status = 'Draft'
                BEGIN
                    SELECT RAISE(ABORT, 'Insufficient stock for invoice posting')
                    WHERE EXISTS (
                        SELECT 1 FROM invoice_items ii
                        JOIN inventory inv ON ii.ItemID = inv.ItemID
                        WHERE ii.InvoiceID = NEW.InvoiceID
                        AND inv.Quantity < ii.Quantity
                    );
                END
            """)
            logging.info("Stock validation trigger created successfully")
        except Exception:
            logging.exception("Error creating stock validation trigger")
        
        conn.commit()

# ==================== DATA OPERATIONS ====================
def load_table(table_name, include_inactive=False):
    """Load table as DataFrame from SQLite."""
    with get_db() as conn:
        query = f'SELECT * FROM "{table_name}"'
        if not include_inactive and table_name in ['customers', 'vendors', 'inventory']:
            query += ' WHERE Active = "Yes"'
        try:
            return pd.read_sql_query(query, conn).fillna("")
        except Exception as e:
            logging.exception("Failed to load table %s: %s", table_name, e)
            # Table might not exist yet or read error
            return pd.DataFrame()

def save_table(table_name, df, use_transaction=True):
    """Save DataFrame to SQLite table using upsert logic to preserve constraints.
    
    Args:
        table_name: Name of the table to save to
        df: DataFrame to save
        use_transaction: If True (default), wrap operation in explicit transaction
    """
    with get_db() as conn:
        if use_transaction:
            conn.execute("BEGIN TRANSACTION")
        
        # Get primary key column name
        pk_query = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        pk_col = next((col[1] for col in pk_query if col[5] == 1), None)
        
        if not pk_col:
            # Fallback to replace if no primary key
            df.to_sql(table_name, conn, if_exists='replace', index=False)
            return
        
        # Get existing IDs
        existing_df = pd.read_sql_query(f'SELECT {pk_col} FROM "{table_name}"', conn)
        existing_ids = set(existing_df[pk_col].tolist())
        
        # Split into updates and inserts
        df_update = df[df[pk_col].isin(existing_ids)]
        df_insert = df[~df[pk_col].isin(existing_ids)]
        
        # Insert new rows
        if not df_insert.empty:
            try:
                df_insert.to_sql(table_name, conn, if_exists="append", index=False)
            except sqlite3.IntegrityError as e:
                # Fall back to per-row INSERT OR REPLACE to handle UNIQUE constraint conflicts
                logging.warning("Bulk insert failed for %s, falling back to per-row upsert: %s", table_name, e)
                cols = df_insert.columns.tolist()
                col_list = ", ".join([f'"{c}"' for c in cols])
                placeholders = ",".join(["?" for _ in cols])
                insert_sql = f'INSERT OR REPLACE INTO "{table_name}" ({col_list}) VALUES ({placeholders})'
                data = [tuple(row[c] for c in cols) for _, row in df_insert.iterrows()]
                try:
                    conn.executemany(insert_sql, data)
                except Exception:
                    logging.exception("Per-row upsert also failed for %s", table_name)
        
        # Update existing rows
        for _, row in df_update.iterrows():
            cols = [c for c in df.columns if c != pk_col]
            set_clause = ", ".join([f"{c} = ?" for c in cols])
            values = [row[c] for c in cols] + [row[pk_col]]
            conn.execute(f"UPDATE {table_name} SET {set_clause} WHERE {pk_col} = ?", values)
        
        if use_transaction:
            conn.commit()

def get_next_id(prefix, table_name, id_column=None):
    """Generate next sequential ID with clean format (e.g., CUST-001, INV-001)."""
    with get_db() as conn:
        # Determine ID column name
        if not id_column:
            pk_query = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            id_column = next((col[1] for col in pk_query if col[5] == 1), None)
        
        if not id_column:
            # Fallback to count
            count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0] + 1
            return f"{prefix}-{str(count).zfill(3)}"
        
        # Get all existing IDs
        result = conn.execute(f'SELECT {id_column} FROM "{table_name}"').fetchall()
        
        if not result:
            return f"{prefix}-001"
        
        # Extract numeric parts from IDs matching the prefix pattern
        max_num = 0
        pattern = re.compile(rf"{re.escape(prefix)}-(\d+)")
        for (id_val,) in result:
            match = pattern.match(str(id_val))
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
        
        next_num = max_num + 1
        return f"{prefix}-{str(next_num).zfill(3)}"

def get_customer_price(customer_id, item_id):
    """Get custom price for customer-item, else return default sell price."""
    with get_db() as conn:
        result = conn.execute(
            "SELECT CustomPrice FROM customer_item_prices WHERE CustomerID = ? AND ItemID = ?",
            (customer_id, item_id)
        ).fetchone()
    
    if result:
        return Decimal(str(result[0]))
    
    # Return default sell price
    inventory = load_table("inventory")
    item = inventory[inventory["ItemID"] == item_id]
    if not item.empty:
        return to_decimal_safe(item.iloc[0]["SellPrice"])
    return Decimal("0.00")

def save_customer_item_price(customer_id, item_id, price):
    """Save or update customer-specific item price."""
    price_id = f"{customer_id}_{item_id}"
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO customer_item_prices (PriceID, CustomerID, ItemID, CustomPrice)
            VALUES (?, ?, ?, ?)
        """, (price_id, customer_id, item_id, float(price)))
        conn.commit()


def get_vendor_price(vendor_id, item_id):
    """Get locked vendor price for item, else return Decimal('0.00')."""
    with get_db() as conn:
        result = conn.execute(
            "SELECT LockedPrice FROM vendor_item_prices WHERE VendorID = ? AND ItemID = ?",
            (vendor_id, item_id)
        ).fetchone()

    if result:
        return Decimal(str(result[0]))

    return Decimal("0.00")


def save_vendor_item_price(vendor_id, item_id, price):
    """Save or update vendor-specific locked price."""
    price_id = f"{vendor_id}_{item_id}"
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO vendor_item_prices (PriceID, VendorID, ItemID, LockedPrice)
            VALUES (?, ?, ?, ?)
        """, (price_id, vendor_id, item_id, float(price)))
        conn.commit()


def delete_vendor_item_price(vendor_id, item_id):
    """Delete a vendor locked price (used when user clears the price to 0)."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM vendor_item_prices WHERE VendorID = ? AND ItemID = ?",
            (vendor_id, item_id)
        )
        conn.commit()

def get_pricing_matrix():
    """Get full pricing matrix for all customers and items."""
    inventory = load_table("inventory")
    customers = load_table("customers")
    
    if inventory.empty or customers.empty:
        return pd.DataFrame()
    
    # Build matrix data
    matrix_data = []
    for _, item in inventory.iterrows():
        row = {
            "ItemID": item["ItemID"],
            "ItemName": item["ItemName"],
            "Quantity": int(item["Quantity"]),
            "CostPrice": to_decimal_safe(item["CostPrice"]),
            "DefaultPrice": to_decimal_safe(item["SellPrice"])
        }
        
        # Add each customer's price
        for _, customer in customers.iterrows():
            custom_price = get_customer_price(customer["CustomerID"], item["ItemID"])
            row[customer["CustomerName"]] = to_decimal_safe(custom_price)
        
        matrix_data.append(row)
    
    return pd.DataFrame(matrix_data)

def save_pricing_matrix(edited_df, customers):
    """Save all pricing changes from matrix."""
    inventory = load_table("inventory")
    saved_count = 0
    
    for idx, row in edited_df.iterrows():
        item_id = row["ItemID"]
        
        for _, customer in customers.iterrows():
            customer_name = customer["CustomerName"]
            customer_id = customer["CustomerID"]
            
            if customer_name in row:
                new_price = Decimal(str(row[customer_name]))
                if new_price > 0:
                    save_customer_item_price(customer_id, item_id, new_price)
                    saved_count += 1
    
    return saved_count

def save_invoice_line_items(invoice_id, items):
    """Save invoice line items to relational table (dual-write with ItemsJSON)."""
    with get_db() as conn:
        # Clear existing line items for this invoice
        conn.execute("DELETE FROM invoice_items WHERE InvoiceID = ?", (invoice_id,))
        
        # Insert new line items
        for idx, item in enumerate(items):
            line_id = f"{invoice_id}-L{idx+1:03d}"
            conn.execute("""
                INSERT INTO invoice_items (LineID, InvoiceID, ItemID, ItemName, Quantity, Price, Total)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                line_id,
                invoice_id,
                item.get('item_id'),
                item.get('item') or item.get('name'),
                int(item.get('qty', 0)),
                str(to_decimal_safe(item.get('price', 0))),
                str(to_decimal_safe(item.get('total', 0)))
            ))
        conn.commit()

def save_bill_line_items(bill_id, items):
    """Save bill line items to relational table (dual-write with ItemsJSON)."""
    with get_db() as conn:
        # Clear existing line items for this bill
        conn.execute("DELETE FROM bill_items WHERE BillID = ?", (bill_id,))
        
        # Insert new line items
        for idx, item in enumerate(items):
            line_id = f"{bill_id}-L{idx+1:03d}"
            conn.execute("""
                INSERT INTO bill_items (LineID, BillID, ItemID, ItemName, Quantity, Price, Total)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                line_id,
                bill_id,
                item.get('item_id'),
                item.get('item') or item.get('name'),
                int(item.get('qty', 0)),
                str(to_decimal_safe(item.get('price', 0))),
                str(to_decimal_safe(item.get('total', 0)))
            ))
        conn.commit()

def get_invoice_line_items(invoice_id):
    """Get invoice line items from relational table, fallback to ItemsJSON."""
    with get_db() as conn:
        # Try relational table first
        rows = conn.execute("""
            SELECT ItemID, ItemName, Quantity, Price, Total
            FROM invoice_items
            WHERE InvoiceID = ?
            ORDER BY LineID
        """, (invoice_id,)).fetchall()
        
        if rows:
            return [{"item_id": r[0], "item": r[1], "qty": r[2], "price": r[3], "total": r[4]} for r in rows]
        
        # Fallback to ItemsJSON for legacy data
        invoice = conn.execute("SELECT ItemsJSON FROM invoices WHERE InvoiceID = ?", (invoice_id,)).fetchone()
        if invoice:
            return parse_items_json(invoice[0])
        return []

def get_bill_line_items(bill_id):
    """Get bill line items from relational table, fallback to ItemsJSON."""
    with get_db() as conn:
        # Try relational table first
        rows = conn.execute("""
            SELECT ItemID, ItemName, Quantity, Price, Total
            FROM bill_items
            WHERE BillID = ?
            ORDER BY LineID
        """, (bill_id,)).fetchall()
        
        if rows:
            return [{"item_id": r[0], "item": r[1], "qty": r[2], "price": r[3], "total": r[4]} for r in rows]
        
        # Fallback to ItemsJSON for legacy data
        bill = conn.execute("SELECT ItemsJSON FROM bills WHERE BillID = ?", (bill_id,)).fetchone()
        if bill:
            return parse_items_json(bill[0])
        return []

def get_estimate_line_items(estimate_id):
    """Get estimate line items from relational table, fallback to ItemsJSON."""
    with get_db() as conn:
        # Try relational table first
        rows = conn.execute("""
            SELECT ItemID, ItemName, Quantity, Price, Total
            FROM estimate_items
            WHERE EstimateID = ?
            ORDER BY LineID
        """, (estimate_id,)).fetchall()
        
        if rows:
            return [{"item_id": r[0], "item": r[1], "qty": r[2], "price": r[3], "total": r[4]} for r in rows]
        
        # Fallback to ItemsJSON for legacy data
        estimate = conn.execute("SELECT ItemsJSON FROM estimates WHERE EstimateID = ?", (estimate_id,)).fetchone()
        if estimate:
            return parse_items_json(estimate[0])
        return []

def save_estimate_line_items(estimate_id, items):
    """Save estimate line items to relational table (dual-write with ItemsJSON)."""
    with get_db() as conn:
        # Clear existing line items for this estimate
        conn.execute("DELETE FROM estimate_items WHERE EstimateID = ?", (estimate_id,))
        
        # Insert new line items
        for idx, item in enumerate(items):
            line_id = f"{estimate_id}-L{idx+1:03d}"
            conn.execute("""
                INSERT INTO estimate_items (LineID, EstimateID, ItemID, ItemName, Quantity, Price, Total)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                line_id,
                estimate_id,
                item.get('item_id'),
                item.get('item') or item.get('name'),
                int(item.get('qty', 0)),
                str(to_decimal_safe(item.get('price', 0))),
                str(to_decimal_safe(item.get('total', 0)))
            ))
        conn.commit()

# ==================== CRUD HELPERS ====================
def can_delete_customer(customer_id):
    """Check if customer can be deleted (no invoices or estimates)."""
    with get_db() as conn:
        invoices = conn.execute("SELECT COUNT(*) FROM invoices WHERE CustomerID = ?", (customer_id,)).fetchone()[0]
        estimates = conn.execute("SELECT COUNT(*) FROM estimates WHERE CustomerID = ?", (customer_id,)).fetchone()[0]
    return invoices == 0 and estimates == 0

def can_delete_vendor(vendor_id):
    """Check if vendor can be deleted (no bills)."""
    with get_db() as conn:
        bills = conn.execute("SELECT COUNT(*) FROM bills WHERE VendorID = ?", (vendor_id,)).fetchone()[0]
    return bills == 0

def can_delete_item(item_id):
    """Check if inventory item can be deleted (not in any invoices/bills).
    
    Uses relational line item tables for proper referential integrity checking.
    Falls back to JSON parsing for backward compatibility with old data.
    """
    with get_db() as conn:
        # Check relational tables first (proper foreign key enforcement)
        invoice_items = conn.execute(
            "SELECT COUNT(*) FROM invoice_items WHERE ItemID = ?",
            (item_id,)
        ).fetchone()[0]
        
        if invoice_items > 0:
            return False
        
        bill_items = conn.execute(
            "SELECT COUNT(*) FROM bill_items WHERE ItemID = ?",
            (item_id,)
        ).fetchone()[0]
        
        if bill_items > 0:
            return False
        
        estimate_items = conn.execute(
            "SELECT COUNT(*) FROM estimate_items WHERE ItemID = ?",
            (item_id,)
        ).fetchone()[0]
        
        if estimate_items > 0:
            return False
        
        # Fallback: Check legacy ItemsJSON (for backward compatibility)
        # This can be removed once full migration to relational tables is complete
        inventory = load_table("inventory")
        item_name = None
        item_match = inventory[inventory["ItemID"] == item_id]
        if not item_match.empty:
            item_name = item_match.iloc[0]["ItemName"]
        
        if item_name:
            # Check if any invoices/bills still use ItemsJSON with this item
            invoices = conn.execute("SELECT ItemsJSON FROM invoices").fetchall()
            for (items_json,) in invoices:
                try:
                    items = json.loads(items_json)
                    for item in items:
                        if item.get('item_id') == item_id or item.get('item') == item_name:
                            return False
                except Exception:
                    continue
            
            bills = conn.execute("SELECT ItemsJSON FROM bills").fetchall()
            for (items_json,) in bills:
                try:
                    items = json.loads(items_json)
                    for item in items:
                        if item.get('item_id') == item_id or item.get('item') == item_name:
                            return False
                except Exception:
                    continue
    
    return True

def soft_delete_record(table_name, id_column, record_id):
    """Soft delete a record by setting Active='No'."""
    with get_db() as conn:
        conn.execute(f"UPDATE {table_name} SET Active = 'No' WHERE {id_column} = ?", (record_id,))
        conn.commit()

def update_record(table_name, id_column, record_id, updates):
    """Update a record with given field values."""
    with get_db() as conn:
        set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [record_id]
        conn.execute(f"UPDATE {table_name} SET {set_clause} WHERE {id_column} = ?", values)
        conn.commit()

def import_inventory_from_file(file):
    """Import inventory from CSV or Excel file."""
    try:
        # Read file
        if file.name.endswith('.csv'):
            df_import = pd.read_csv(file)
        elif file.name.endswith(('.xlsx', '.xls')):
            df_import = pd.read_excel(file)
        else:
            return None, "Unsupported file format. Use CSV or Excel."
        
        # Validate columns (accept both LowStockThreshold and ReorderLevel)
        required_cols = ['ItemName', 'CostPrice', 'SellPrice', 'Quantity']
        missing = [c for c in required_cols if c not in df_import.columns]
        if missing:
            return None, f"Missing columns: {', '.join(missing)}"
        
        # Accept either LowStockThreshold or ReorderLevel column
        reorder_col = 'ReorderLevel' if 'ReorderLevel' in df_import.columns else 'LowStockThreshold'
        if reorder_col not in df_import.columns:
            df_import[reorder_col] = 10
        
        required_cols.append(reorder_col)
        
        # Clean and validate data
        df_import = df_import[required_cols].copy()
        df_import['ItemName'] = df_import['ItemName'].astype(str).str.strip()
        df_import['CostPrice'] = pd.to_numeric(df_import['CostPrice'], errors='coerce').fillna(0)
        df_import['SellPrice'] = pd.to_numeric(df_import['SellPrice'], errors='coerce').fillna(0)
        df_import['Quantity'] = pd.to_numeric(df_import['Quantity'], errors='coerce').fillna(0).astype(int)
        df_import[reorder_col] = pd.to_numeric(df_import[reorder_col], errors='coerce').fillna(10).astype(int)
        # Rename to ReorderLevel for consistency
        if reorder_col == 'LowStockThreshold':
            df_import.rename(columns={'LowStockThreshold': 'ReorderLevel'}, inplace=True)
        
        # Remove empty names
        df_import = df_import[df_import['ItemName'] != '']
        
        return df_import, None
    except Exception as e:
        return None, f"Error reading file: {str(e)}"

def apply_inventory_import(df_import):
    """Apply inventory import by inserting/updating items."""
    try:
        inventory = load_table("inventory", include_inactive=True)
        
        new_count = 0
        updated_count = 0
        
        for _, row in df_import.iterrows():
            item_name = row['ItemName']
            existing = inventory[inventory['ItemName'] == item_name]
            
            if existing.empty:
                # New item
                item_id = f"ITM-{datetime.now().strftime('%Y%m%d%H%M%S')}-{new_count}"
                new_item = {
                    'ItemID': item_id,
                    'ItemName': item_name,
                    'Quantity': int(row['Quantity']),
                    'CostPrice': str(to_decimal_safe(row['CostPrice'])),
                    'SellPrice': str(to_decimal_safe(row['SellPrice'])),
                    'ReorderLevel': int(row['ReorderLevel']),
                    'Active': 'Yes'
                }
                inventory = pd.concat([inventory, pd.DataFrame([new_item])], ignore_index=True)
                new_count += 1
            else:
                # Update existing
                idx = existing.index[0]
                inventory.at[idx, 'Quantity'] = int(row['Quantity'])
                inventory.at[idx, 'CostPrice'] = str(to_decimal_safe(row['CostPrice']))
                inventory.at[idx, 'SellPrice'] = str(to_decimal_safe(row['SellPrice']))
                inventory.at[idx, 'ReorderLevel'] = int(row['ReorderLevel'])
                inventory.at[idx, 'Active'] = 'Yes'
                updated_count += 1
        
        save_table("inventory", inventory)
        return True, f"✅ Import successful: {new_count} new items, {updated_count} updated"
    except Exception as e:
        return False, f"Error importing: {str(e)}"

def create_database_backup():
    """Create a backup of the database file."""
    try:
        db_path = get_active_db_path()
        # Checkpoint WAL to ensure all data is in main DB file
        with _get_db_for_path(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        
        backup_data = BytesIO()
        with open(db_path, 'rb') as f:
            backup_data.write(f.read())
        backup_data.seek(0)
        return backup_data, None
    except Exception as e:
        return None, f"Backup failed: {str(e)}"

def create_full_export_zip():
    """Export all data as CSV files in a ZIP."""
    try:
        import zipfile
        
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Export tables
            tables = ['inventory', 'customers', 'vendors', 'invoices', 'bills', 'expenses', 'estimates']
            for table in tables:
                try:
                    df = load_table(table, include_inactive=True)
                    csv_data = df.to_csv(index=False)
                    zip_file.writestr(f"{table}.csv", csv_data)
                except Exception:
                    logging.exception("Failed to export table %s", table)
        
        zip_buffer.seek(0)
        return zip_buffer, None
    except Exception as e:
        return None, f"Export failed: {str(e)}"

def get_backup_config(key):
    """Get backup configuration value."""
    try:
        with get_db() as conn:
            result = conn.execute("SELECT ConfigValue FROM backup_config WHERE ConfigKey = ?", (key,)).fetchone()
            return result[0] if result else None
    except Exception:
        logging.exception("get_backup_config failed for key %s", key)
        return None

def set_backup_config(key, value):
    """Set backup configuration value."""
    try:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO backup_config (ConfigKey, ConfigValue) VALUES (?, ?)", (key, value))
            conn.commit()
        return True
    except Exception:
        logging.exception("set_backup_config failed for key %s", key)
        return False

def reset_transactional_data():
    """
    Reset all transactional data while keeping master data.
    Keeps: Customers, Vendors, Inventory (with balances and prices)
    Deletes: Invoices, Bills, Estimates, Purchase Orders, Expenses, Payments, Loan Transactions
    """
    try:
        with get_db() as conn:
            # Delete all transactional tables
            tables_to_clear = [
                'invoices',
                'bills', 
                'estimates',
                'purchase_orders',
                'expenses',
                'customer_payments',
                'vendor_payments',
                'loan_transactions',
                'invoice_items',
                'bill_items',
                'estimate_items'
            ]
            
            for table in tables_to_clear:
                try:
                    conn.execute(f"DELETE FROM {table}")
                except Exception as e:
                    logging.exception(f"Failed to clear table {table}: {e}")
            
            conn.commit()
            return True, "All transactional data deleted successfully. Master data (Customers, Vendors, Inventory, Loan Parties) preserved."
    except Exception as e:
        logging.exception("reset_transactional_data failed")
        return False, f"Reset failed: {str(e)}"

def update_balance_precise(conn, table: str, id_column: str, id_value: str, amount: Decimal, operation: str = "add"):
    """
    Update financial balance with exact Decimal precision.
    
    Reads current balance as TEXT, performs Python Decimal arithmetic, writes back as TEXT.
    This avoids SQLite REAL precision loss.
    
    Args:
        conn: Database connection
        table: customers, vendors, or loans
        id_column: CustomerID, VendorID, or LoanID
        id_value: The ID value
        amount: Decimal amount to add or subtract
        operation: 'add' or 'subtract'
    """
    try:
        # Read current balance as TEXT
        current = conn.execute(
            f"SELECT OpenBalance FROM {table} WHERE {id_column} = ?",
            (id_value,)
        ).fetchone()
        
        if not current:
            raise ValueError(f"{id_column} {id_value} not found in {table}")
        
        current_balance = to_decimal_safe(current[0], Decimal("0.00"))
        
        # Perform precise Decimal arithmetic in Python
        if operation == "add":
            new_balance = current_balance + amount
        elif operation == "subtract":
            new_balance = current_balance - amount
        else:
            raise ValueError(f"Invalid operation: {operation}")
        
        # Write back as TEXT string
        conn.execute(
            f"UPDATE {table} SET OpenBalance = ? WHERE {id_column} = ?",
            (str(new_balance), id_value)
        )
        
        return new_balance
    except Exception as e:
        logging.exception(f"update_balance_precise failed for {table}.{id_column}={id_value}")
        raise e

def perform_auto_backup():
    """Perform automatic daily backup if enabled."""
    try:
        db_path = get_active_db_path()
        # Check if auto-backup is enabled
        if get_backup_config("auto_backup_enabled") != "Yes":
            return None, "Auto-backup is disabled"
        
        # Check last backup date
        last_backup = get_backup_config("last_backup_date")
        today = datetime.now().strftime("%Y-%m-%d")
        
        if last_backup == today:
            return None, "Backup already performed today"
        
        # Create backup directory
        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(exist_ok=True)
        
        # Create backup file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = backup_dir / f"auto_backup_{timestamp}.db"
        
        # Checkpoint WAL before backup
        with _get_db_for_path(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        
        # Copy database
        shutil.copy2(db_path, backup_filename)
        
        # Update last backup date
        set_backup_config("last_backup_date", today)
        
        # Clean old backups (keep last 7 days)
        cleanup_old_backups(backup_dir, days=7)
        
        return str(backup_filename), None
    except Exception as e:
        return None, f"Auto-backup failed: {str(e)}"

def cleanup_old_backups(backup_dir, days=7):
    """Remove backups older than specified days."""
    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        
        for backup_file in backup_dir.glob("auto_backup_*.db"):
            # Extract timestamp from filename
            try:
                timestamp_str = backup_file.stem.replace("auto_backup_", "")
                file_date = datetime.strptime(timestamp_str[:8], "%Y%m%d")
                
                if file_date < cutoff_date:
                    backup_file.unlink()
            except Exception:
                logging.exception("Failed processing backup file %s", backup_file)
    except Exception:
        logging.exception("cleanup_old_backups failed")

def update_customer_balance(customer_id, amount):
    """Update customer open balance with exact Decimal precision."""
    with get_db() as conn:
        update_balance_precise(conn, "customers", "CustomerID", customer_id, to_decimal_safe(amount), "add")
        conn.commit()

def update_vendor_balance(vendor_id, amount):
    """Update vendor open balance with exact Decimal precision."""
    with get_db() as conn:
        update_balance_precise(conn, "vendors", "VendorID", vendor_id, to_decimal_safe(amount), "add")
        conn.commit()

def update_loan_balance(party_id, amount):
    """Update loan party balance with exact Decimal precision. Positive amount increases what we owe them; negative reduces it."""
    with get_db() as conn:
        # Note: loan_parties table uses 'Balance' column, not 'OpenBalance'
        current = conn.execute("SELECT Balance FROM loan_parties WHERE PartyID = ?", (party_id,)).fetchone()
        if not current:
            raise ValueError(f"Party {party_id} not found")
        
        current_balance = to_decimal_safe(current[0], Decimal("0.00"))
        new_balance = current_balance + to_decimal_safe(amount)
        
        conn.execute(
            "UPDATE loan_parties SET Balance = ? WHERE PartyID = ?",
            (str(new_balance), party_id)
        )
        conn.commit()

def record_loan_transaction(party_id, amount, direction, notes=""):
    """Record a loan transaction and update the party balance.

    direction: 'Received' = we received a loan (increase party balance),
               'Given' = we gave a loan (decrease party balance)
    """
    try:
        tx_id = get_next_id("LTX", "loan_transactions", "TransactionID")
        amount_decimal = to_decimal_safe(amount)
        
        # Atomic transaction: insert transaction + update balance
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO loan_transactions (TransactionID, Date, PartyID, Amount, Direction, Notes) VALUES (?, ?, ?, ?, ?, ?)",
                    (tx_id, datetime.now().strftime("%Y-%m-%d"), party_id, str(amount_decimal), direction, notes)
                )
                
                # Apply balance change: Received -> +amount, Given -> -amount
                delta = amount_decimal if direction == 'Received' else (amount_decimal * Decimal("-1"))
                conn.execute(
                    "UPDATE loan_parties SET Balance = Balance + ? WHERE PartyID = ?",
                    (str(delta), party_id)
                )
                
                conn.commit()
                return True, f"Loan transaction {tx_id} recorded"
            except Exception as e:
                conn.rollback()
                raise e
    except Exception as e:
        logging.exception("Loan transaction recording failed")
        return False, f"Error: {str(e)}"

def calculate_cogs_from_invoices(invoices_df):
    """Calculate actual Cost of Goods Sold from invoice items."""
    if invoices_df.empty:
        return Decimal("0.00")
    
    # Include inactive items so historical invoices still compute cost correctly.
    inventory = load_table("inventory", include_inactive=True)
    total_cogs = Decimal("0.00")

    # Build quick lookup maps to resolve legacy rows that may have missing ItemID.
    name_to_id = {}
    id_to_cost = {}
    if not inventory.empty:
        for _, row in inventory.iterrows():
            item_id = str(row.get("ItemID", "")).strip()
            item_name = str(row.get("ItemName", "")).strip()
            if item_name and item_id and item_name not in name_to_id:
                name_to_id[item_name] = item_id
            if item_id and item_id not in id_to_cost:
                id_to_cost[item_id] = to_decimal_safe(row.get("CostPrice", 0), Decimal("0.00"))
    
    # Load and cache all posted bills once for performance
    bills_cache = load_table("bills")
    if not bills_cache.empty:
        bills_cache = bills_cache[bills_cache['Status'].isin(['Posted', 'Paid'])].sort_values(by='Date', ascending=False)
    
    def get_historical_cost(item_id, item_name, invoice_date, bills_df):
        """Return most recent posted bill unit price for this item on/before invoice_date, or None.

        Tries ItemID match first, then falls back to ItemName match for legacy rows.
        """
        try:
            if bills_df.empty:
                return None
            invoice_date = str(invoice_date or "9999-12-31")
            # Filter bills up to invoice_date
            relevant_bills = bills_df[(bills_df['Date'] != "") & (bills_df['Date'] <= invoice_date)]
            if relevant_bills.empty:
                return None
            # Try relational table first (preferred), then fallback to ItemsJSON
            with get_db() as conn:
                for _, bill in relevant_bills.iterrows():
                    bill_id = bill['BillID']
                    
                    # Query bill_items table for this item
                    if item_id:
                        result = conn.execute("""
                            SELECT Price FROM bill_items 
                            WHERE BillID = ? AND ItemID = ?
                            ORDER BY LineID
                            LIMIT 1
                        """, (bill_id, item_id)).fetchone()
                        if result:
                            return to_decimal_safe(result[0])

                    # Fallback by item name for legacy bills/items that have no ItemID
                    if item_name:
                        result = conn.execute("""
                            SELECT Price FROM bill_items 
                            WHERE BillID = ? AND ItemName = ?
                            ORDER BY LineID
                            LIMIT 1
                        """, (bill_id, item_name)).fetchone()
                        if result:
                            return to_decimal_safe(result[0])
                    
                    # Fallback to JSON for legacy data
                    try:
                        items = json.loads(bill.get('ItemsJSON', '[]'))
                        for it in items:
                            bill_item_id = (it.get('item_id') or it.get('ItemID') or "")
                            bill_item_name = (it.get('item') or it.get('Item') or it.get('ItemName') or "")
                            if item_id and str(bill_item_id) == str(item_id):
                                return to_decimal_safe(it.get('price') or it.get('Price') or 0)
                            if item_name and str(bill_item_name).strip() == str(item_name).strip():
                                return to_decimal_safe(it.get('price') or it.get('Price') or 0)
                    except Exception:
                        continue
            return None
        except Exception:
            logging.exception("get_historical_cost failed for %s", item_id)
            return None

    for _, invoice in invoices_df.iterrows():
        invoice_id = invoice.get('InvoiceID')
        invoice_date = invoice.get('Date', '9999-12-31')
        
        # Read from relational table (preferred) or JSON fallback
        try:
            items = get_invoice_line_items(invoice_id) if invoice_id else parse_items_json(invoice.get("ItemsJSON", "[]"))
            for item in items:
                item_id = str(item.get("item_id") or item.get('ItemID') or "").strip()
                item_name = str(item.get("item") or item.get("Item") or item.get("ItemName") or "").strip()
                if not item_id and item_name:
                    item_id = name_to_id.get(item_name, "")

                quantity = to_decimal_safe(item.get("qty", item.get("quantity", item.get("Qty", 0))), Decimal("0.00"))

                # Prefer historical bill cost at or before invoice date
                cost_price = None
                if item_id or item_name:
                    cost_price = get_historical_cost(item_id or None, item_name or None, invoice_date, bills_cache)

                # Fallback to inventory cost price
                if cost_price is None:
                    if item_id and item_id in id_to_cost:
                        cost_price = id_to_cost[item_id]
                    else:
                        cost_price = Decimal("0.00")

                total_cogs += (quantity * cost_price)
        except Exception as e:
            logging.exception("calculate_cogs_from_invoices: failed to process invoice items: %s", e)
    
    return total_cogs

def record_customer_payment(customer_id, amount, payment_method, notes=""):
    """Record a payment received from customer with atomic transaction."""
    try:
        payment_id = get_next_id("CPAY", "customer_payments", "PaymentID")
        
        # Atomic transaction: insert payment + update balance
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO customer_payments (PaymentID, Date, CustomerID, Amount, PaymentMethod, Notes) VALUES (?, ?, ?, ?, ?, ?)",
                    (payment_id, datetime.now().strftime("%Y-%m-%d"), customer_id, str(to_decimal_safe(amount)), payment_method, notes)
                )
                
                # Decrease customer balance with precise Decimal arithmetic
                update_balance_precise(conn, "customers", "CustomerID", customer_id, to_decimal_safe(amount), "subtract")
                
                conn.commit()
                return True, f"Payment {payment_id} recorded"
            except Exception as e:
                conn.rollback()
                raise e
    except Exception as e:
        logging.exception("Customer payment recording failed")
        return False, f"Error: {str(e)}"

def record_vendor_payment(vendor_id, amount, payment_method, notes=""):
    """Record a payment made to vendor with atomic transaction."""
    try:
        payment_id = get_next_id("VPAY", "vendor_payments", "PaymentID")
        
        # Atomic transaction: insert payment + update balance
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO vendor_payments (PaymentID, Date, VendorID, Amount, PaymentMethod, Notes) VALUES (?, ?, ?, ?, ?, ?)",
                    (payment_id, datetime.now().strftime("%Y-%m-%d"), vendor_id, str(to_decimal_safe(amount)), payment_method, notes)
                )
                
                # Decrease vendor balance with precise Decimal arithmetic
                update_balance_precise(conn, "vendors", "VendorID", vendor_id, to_decimal_safe(amount), "subtract")
                
                conn.commit()
                return True, f"Payment {payment_id} recorded"
            except Exception as e:
                conn.rollback()
                raise e
    except Exception as e:
        logging.exception("Vendor payment recording failed")
        return False, f"Error: {str(e)}"


def render_share_options(*, doc_type: str, doc_id: str, customer_name: str = "", doc_date: str = "", key_prefix: str):
    """Render Share options (WhatsApp + Email).

    Note: These links can't attach files automatically; users download the PDF then attach it.
    """

    customer_part = f" for {customer_name}" if customer_name else ""
    date_part = f" dated {doc_date}" if doc_date else ""
    message = f"{doc_type} {doc_id}{customer_part}{date_part}. Please find the PDF attached."

    wa_url = "https://api.whatsapp.com/send?text=" + quote(message)
    subject = f"{doc_type} {doc_id}"
    body = message + "\n\n(Download the PDF from the app and attach it here.)"
    mailto_url = "mailto:?subject=" + quote(subject) + "&body=" + quote(body)

    label = f"📤 {t('share')} {doc_type}"

    # Prefer popover: clean UI and never "already opened"
    if hasattr(st, "popover"):
        with st.popover(label, use_container_width=True):
            if hasattr(st, "link_button"):
                st.link_button(f"🟢 {t('whatsapp')}", wa_url, use_container_width=True)
                st.link_button(f"✉️ {t('email')}", mailto_url, use_container_width=True)
            else:
                st.markdown(f"[🟢 {t('whatsapp')}]({wa_url})")
                st.markdown(f"[✉️ {t('email')}]({mailto_url})")
        return

    # Fallback: open once, then auto-close on next rerun
    state_key = f"share_open_{key_prefix}"
    if state_key not in st.session_state:
        st.session_state[state_key] = False

    if st.button(label, key=f"btn_share_{key_prefix}", width="stretch"):
        st.session_state[state_key] = True

    open_now = bool(st.session_state.get(state_key, False))
    if not open_now:
        return

    # Auto-close for future reruns; does not affect current render
    st.session_state[state_key] = False

    with st.container(border=True):
        if hasattr(st, "link_button"):
            st.link_button(f"🟢 {t('whatsapp')}", wa_url, use_container_width=True)
            st.link_button(f"✉️ {t('email')}", mailto_url, use_container_width=True)
        else:
            st.markdown(f"[🟢 {t('whatsapp')}]({wa_url})")
            st.markdown(f"[✉️ {t('email')}]({mailto_url})")


def _pdf_to_bytes(pdf_obj) -> bytes:
    """Normalize PDF output to raw bytes.

    Some generators return BytesIO, others return raw bytes.
    """
    if pdf_obj is None:
        return b""
    if isinstance(pdf_obj, (bytes, bytearray)):
        return bytes(pdf_obj)
    if hasattr(pdf_obj, "getvalue"):
        return pdf_obj.getvalue()
    if hasattr(pdf_obj, "read"):
        return pdf_obj.read()
    raise TypeError(f"Unsupported PDF object type: {type(pdf_obj)!r}")

# ==================== PDF GENERATION ====================
def generate_professional_invoice_pdf(invoice_data, items, company_info=None):
    """
    Generate a professional invoice PDF in memory using BytesIO.
    
    Returns BytesIO object containing the PDF.
    """
    pdf = FPDF()
    pdf.add_page()
    
    # Page border
    pdf.set_draw_color(0, 0, 0)
    pdf.rect(5, 5, 200, 287)
    
    # Company Logo (if exists) - Enlarged
    logo_path = get_company_logo_path(get_active_company_name())
    if logo_path.exists():
        pdf.image(str(logo_path), x=10, y=8, w=50)
    
    # Company Info (Top Left)
    pdf.set_font("Arial", "B", 16)
    pdf.set_xy(10, 45)
    pdf.cell(0, 8, get_active_company_name(), 0, 1)
    
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 5, "Basement No.11", 0, 1)
    pdf.cell(0, 5, "Office 13B Khyber Plaza, Fazal e Haq Road", 0, 1)
    pdf.cell(0, 5, "Blue Area, Islamabad.", 0, 1)
    pdf.cell(0, 5, f"NTN No: {NTN_NUMBER}", 0, 1)
    if company_info:
        pdf.cell(0, 5, company_info.get("address", ""), 0, 1)
        pdf.cell(0, 5, company_info.get("phone", ""), 0, 1)
        pdf.cell(0, 5, company_info.get("email", ""), 0, 1)
    
    # INVOICE Title (Top Right)
    pdf.set_font("Arial", "B", 24)
    pdf.set_xy(140, 15)
    pdf.cell(60, 10, "INVOICE", 0, 1, 'R')
    
    # Invoice Details (Top Right)
    pdf.set_font("Arial", "", 10)
    pdf.set_xy(140, 25)
    pdf.cell(60, 5, f"Invoice #: {invoice_data.get('InvoiceID', '')}", 0, 1, 'R')
    pdf.set_xy(140, 30)
    pdf.cell(60, 5, f"Date: {invoice_data.get('Date', '')}", 0, 1, 'R')
    pdf.set_xy(140, 35)
    pdf.cell(60, 5, f"Status: {invoice_data.get('Status', 'Open')}", 0, 1, 'R')
    
    # Bill To Section
    pdf.set_xy(10, 75)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 6, "BILL TO:", 0, 1)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 5, invoice_data.get('Customer', invoice_data.get('CustomerName', '')), 0, 1)
    pdf.cell(0, 5, "", 0, 1)  # Address placeholder
    
    # Items Table
    pdf.set_xy(10, 100)
    pdf.set_font("Arial", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    
    # Table Headers
    has_tax = invoice_data.get('TaxType') == 'With Tax'
    tax_rate = Decimal(str(invoice_data.get('TaxRate', 0)))
    
    # Table Rows with page break and header repeat
    pdf.set_font("Arial", "", 10)
    max_y = 265  # bottom margin for table
    def print_invoice_table_header():
        pdf.set_font("Arial", "B", 10)
        pdf.set_fill_color(240, 240, 240)
        if has_tax:
            pdf.cell(65, 8, "Item", 1, 0, 'L', True)
            pdf.cell(20, 8, "Qty", 1, 0, 'C', True)
            pdf.cell(25, 8, "Unit Price", 1, 0, 'R', True)
            pdf.cell(25, 8, "Subtotal", 1, 0, 'R', True)
            pdf.cell(25, 8, "Tax", 1, 0, 'R', True)
            pdf.cell(30, 8, "Total", 1, 1, 'R', True)
        else:
            pdf.cell(80, 8, "Item", 1, 0, 'L', True)
            pdf.cell(20, 8, "Qty", 1, 0, 'C', True)
            pdf.cell(30, 8, "Unit Price", 1, 0, 'R', True)
            pdf.cell(60, 8, "Total", 1, 1, 'R', True)
        pdf.set_font("Arial", "", 10)
    print_invoice_table_header()
    for item in items:
        if pdf.get_y() > max_y:
            pdf.add_page()
            print_invoice_table_header()
        item_subtotal = Decimal(str(item.get('total', 0)))
        if has_tax:
            item_tax = (item_subtotal * tax_rate / Decimal("100"))
            item_total = item_subtotal + item_tax
            pdf.cell(65, 7, str(item.get('item', ''))[:30], 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(25, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_subtotal:,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_tax:,.0f}", 1, 0, 'R')
            pdf.cell(30, 7, f"Rs {item_total:,.0f}", 1, 1, 'R')
        else:
            pdf.cell(80, 7, str(item.get('item', ''))[:35], 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(30, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(60, 7, f"Rs {item_subtotal:,.0f}", 1, 1, 'R')
    
    # Totals Section
    pdf.ln(5)
    pdf.set_font("Arial", "", 10)
    
    total_x = 140 if has_tax else 130
    pdf.set_xy(total_x, pdf.get_y())
    pdf.cell(30, 6, "Subtotal:", 0, 0, 'R')
    pdf.cell(30, 6, f"Rs {Decimal(str(invoice_data.get('Subtotal', 0))):,.2f}", 0, 1, 'R')
    
    discount = Decimal(str(invoice_data.get('Discount', 0)))
    if discount > 0:
        pdf.set_x(total_x)
        pdf.cell(30, 6, "Discount:", 0, 0, 'R')
        pdf.cell(30, 6, f"Rs {discount:,.2f}", 0, 1, 'R')
    
    if has_tax:
        pdf.set_x(total_x)
        pdf.cell(30, 6, "Tax:", 0, 0, 'R')
        pdf.cell(30, 6, f"Rs {Decimal(str(invoice_data.get('Tax', 0))):,.2f}", 0, 1, 'R')
    
    pdf.ln(2)
    pdf.set_font("Arial", "B", 12)
    pdf.set_x(total_x)
    pdf.cell(30, 8, "TOTAL:", 0, 0, 'R')
    pdf.cell(30, 8, f"Rs {Decimal(str(invoice_data.get('Total', 0))):,.2f}", 0, 1, 'R')
    
    # Notes Section
    pdf.set_xy(10, 160)
    pdf.set_font("Arial", "I", 9)
    pdf.multi_cell(0, 5, "Thank you for your business!", 0, 'L')

    # Software-generated notice at bottom
    pdf.set_auto_page_break(False)
    pdf.set_y(275)
    pdf.set_font("Arial", "I", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 5, "This is a computer-generated invoice. No signature is required.", 0, 'C')
    pdf.set_text_color(0, 0, 0)
    
    pdf_output = BytesIO()
    pdf_output.write(pdf.output(dest='S').encode('latin1'))
    pdf_output.seek(0)
    return pdf_output.getvalue()

def generate_purchase_order_pdf(po_data, items, company_info=None):
    """
    Generate a professional purchase order PDF.
    
    Returns bytes containing the PDF.
    """
    pdf = FPDF()
    pdf.add_page()
    
    # Page border
    pdf.set_draw_color(0, 0, 0)
    pdf.rect(5, 5, 200, 287)
    
    # Company Logo (if exists)
    logo_path = get_company_logo_path(get_active_company_name())
    if logo_path.exists():
        pdf.image(str(logo_path), x=10, y=8, w=50)
    
    # Company Info (Top Left)
    pdf.set_font("Arial", "B", 16)
    pdf.set_xy(10, 45)
    pdf.cell(0, 8, get_active_company_name(), 0, 1)
    
    pdf.set_font("Arial", "", 10)
    if company_info:
        pdf.cell(0, 5, company_info.get("address", ""), 0, 1)
        pdf.cell(0, 5, company_info.get("phone", ""), 0, 1)
        pdf.cell(0, 5, company_info.get("email", ""), 0, 1)
    
    # PURCHASE ORDER Title (Top Right)
    pdf.set_font("Arial", "B", 20)
    pdf.set_xy(130, 15)
    pdf.cell(70, 10, "PURCHASE ORDER", 0, 1, 'R')
    
    # PO Details (Top Right)
    pdf.set_font("Arial", "", 10)
    pdf.set_xy(140, 25)
    pdf.cell(60, 5, f"PO #: {po_data.get('POID', '')}", 0, 1, 'R')
    pdf.set_xy(140, 30)
    pdf.cell(60, 5, f"Date: {po_data.get('Date', '')}", 0, 1, 'R')
    pdf.set_xy(140, 35)
    pdf.cell(60, 5, f"Status: {po_data.get('Status', 'Pending')}", 0, 1, 'R')
    
    # Vendor Section
    pdf.set_xy(10, 75)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 6, "VENDOR:", 0, 1)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 5, po_data.get('Vendor', po_data.get('VendorName', '')), 0, 1)
    pdf.cell(0, 5, "", 0, 1)
    
    # Items Table
    pdf.set_xy(10, 100)
    pdf.set_font("Arial", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    
    # Table Headers
    has_tax = po_data.get('TaxType') == 'With Tax'
    tax_rate = Decimal(str(po_data.get('TaxRate', 0)))
    
    if has_tax:
        pdf.cell(65, 8, "Item", 1, 0, 'L', True)
        pdf.cell(20, 8, "Qty", 1, 0, 'C', True)
        pdf.cell(25, 8, "Unit Price", 1, 0, 'R', True)
        pdf.cell(25, 8, "Subtotal", 1, 0, 'R', True)
        pdf.cell(25, 8, "Tax", 1, 0, 'R', True)
        pdf.cell(30, 8, "Total", 1, 1, 'R', True)
        pass  # Removed duplicate header rendering
    
    # Table Rows
    pdf.set_font("Arial", "", 10)
    for item in items:
        item_subtotal = Decimal(str(item.get('total', 0)))
        
        if has_tax:
            item_tax = (item_subtotal * tax_rate / Decimal("100"))
            item_total = item_subtotal + item_tax
            
            pdf.cell(80, 7, str(item.get('item', '')), 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(25, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_subtotal:,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_tax:,.0f}", 1, 0, 'R')
            pdf.cell(30, 7, f"Rs {item_total:,.0f}", 1, 1, 'R')
        else:
            pdf.cell(80, 7, str(item.get('item', ''))[:35], 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(30, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(60, 7, f"Rs {item_subtotal:,.0f}", 1, 1, 'R')
    
    # Totals Section
    pdf.ln(5)
    pdf.set_font("Arial", "", 10)
    
    total_x = 140 if has_tax else 130
    pdf.set_xy(total_x, pdf.get_y())
    pdf.cell(30, 6, "Subtotal:", 0, 0, 'R')
    pdf.cell(30, 6, f"Rs {Decimal(str(po_data.get('Subtotal', 0))):,.2f}", 0, 1, 'R')
    
    discount = Decimal(str(po_data.get('Discount', 0)))
    if discount > 0:
        pdf.set_x(total_x)
        pdf.cell(30, 6, "Discount:", 0, 0, 'R')
        pdf.cell(30, 6, f"Rs {discount:,.2f}", 0, 1, 'R')
    
    if has_tax:
        pdf.set_x(total_x)
        pdf.cell(30, 6, "Tax:", 0, 0, 'R')
        pdf.cell(30, 6, f"Rs {Decimal(str(po_data.get('Tax', 0))):,.2f}", 0, 1, 'R')
    
    pdf.ln(2)
    pdf.set_font("Arial", "B", 12)
    pdf.set_x(total_x)
    pdf.cell(30, 8, "TOTAL:", 0, 0, 'R')
    pdf.cell(30, 8, f"Rs {Decimal(str(po_data.get('Total', 0))):,.2f}", 0, 1, 'R')
    
    # Notes Section
    pdf.set_xy(10, 160)
    pdf.set_font("Arial", "I", 9)
    if po_data.get('Notes'):
        pdf.multi_cell(0, 5, f"Notes: {po_data.get('Notes')}", 0, 'L')
    
    pdf.ln(5)
    pdf.set_font("Arial", "B", 10)
    pdf.cell(0, 5, "Please deliver the items as per this purchase order.", 0, 1, 'L')
    
    # Software-generated notice at bottom
    pdf.set_auto_page_break(False)
    pdf.set_y(275)
    pdf.set_font("Arial", "I", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 5, "This is a computer-generated invoice. No signature is required.", 0, 'C')
    pdf.set_text_color(0, 0, 0)
    
    # Return PDF as BytesIO
    pdf_output = BytesIO()
    pdf_output.write(pdf.output(dest='S').encode('latin1'))
    pdf_output.seek(0)
    return pdf_output

def generate_professional_estimate_pdf(estimate_data, items, company_info=None):
    """
    Generate a professional estimate PDF in memory using BytesIO.
    
    Includes validity date and client signature line.
    Returns BytesIO object containing the PDF.
    """
    pdf = FPDF()
    pdf.add_page()
    
    # Page border
    pdf.set_draw_color(0, 0, 0)
    pdf.rect(5, 5, 200, 287)
    
    # Company Logo - Enlarged
    logo_path = get_company_logo_path(get_active_company_name())
    if logo_path.exists():
        pdf.image(str(logo_path), x=10, y=8, w=50)
    
    # Company Info
    pdf.set_font("Arial", "B", 16)
    pdf.set_xy(10, 45)
    pdf.cell(0, 8, get_active_company_name(), 0, 1)
    
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 5, "Basement No.11", 0, 1)
    pdf.cell(0, 5, "Office 13B Khyber Plaza, Fazal e Haq Road", 0, 1)
    pdf.cell(0, 5, "Blue Area, Islamabad.", 0, 1)
    pdf.cell(0, 5, f"NTN No: {NTN_NUMBER}", 0, 1)
    if company_info:
        pdf.cell(0, 5, company_info.get("address", ""), 0, 1)
        pdf.cell(0, 5, company_info.get("phone", ""), 0, 1)
    
    # ESTIMATE Title
    pdf.set_font("Arial", "B", 24)
    pdf.set_xy(140, 10)
    pdf.cell(60, 10, "ESTIMATE", 0, 1, 'R')
    
    # Estimate Details
    pdf.set_font("Arial", "", 10)
    pdf.set_xy(140, 25)
    pdf.cell(60, 5, f"Estimate #: {estimate_data.get('EstimateID', '')}", 0, 1, 'R')
    pdf.set_xy(140, 30)
    pdf.cell(60, 5, f"Date: {estimate_data.get('Date', '')}", 0, 1, 'R')
    
    # Valid Until Date (30 days from estimate date)
    from datetime import timedelta
    try:
        est_date = datetime.strptime(estimate_data.get('Date', ''), "%Y-%m-%d")
        valid_until = est_date + timedelta(days=30)
        pdf.set_xy(140, 35)
        pdf.cell(60, 5, f"Valid Until: {valid_until.strftime('%Y-%m-%d')}", 0, 1, 'R')
    except Exception:
        logging.debug("Invalid estimate date format for estimate: %s", estimate_data.get('EstimateID', ''))
    
    pdf.set_xy(140, 40)
    
    # Customer Section
    pdf.set_xy(10, 75)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 6, "PREPARED FOR:", 0, 1)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 5, estimate_data.get('Customer', estimate_data.get('CustomerName', '')), 0, 1)
    pdf.ln(5)
    
    # Items Table
    pdf.set_xy(10, 100)
    pdf.set_font("Arial", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    
    # Table Headers
    has_tax = estimate_data.get('TaxType') == 'With Tax'
    tax_rate = Decimal(str(estimate_data.get('TaxRate', 0)))
    
    if has_tax:
        pass  # Removed duplicate header rendering
    
    # Rows with page break and header repeat
    pdf.set_font("Arial", "", 10)
    max_y = 265
    def print_estimate_table_header():
        pdf.set_font("Arial", "B", 10)
        pdf.set_fill_color(240, 240, 240)
        if has_tax:
            pdf.cell(65, 8, "Item", 1, 0, 'L', True)
            pdf.cell(20, 8, "Qty", 1, 0, 'C', True)
            pdf.cell(25, 8, "Unit Price", 1, 0, 'R', True)
            pdf.cell(25, 8, "Subtotal", 1, 0, 'R', True)
            pdf.cell(25, 8, "Tax", 1, 0, 'R', True)
            pdf.cell(30, 8, "Total", 1, 1, 'R', True)
        else:
            pdf.cell(80, 8, "Item", 1, 0, 'L', True)
            pdf.cell(20, 8, "Qty", 1, 0, 'C', True)
            pdf.cell(30, 8, "Unit Price", 1, 0, 'R', True)
            pdf.cell(60, 8, "Total", 1, 1, 'R', True)
        pdf.set_font("Arial", "", 10)
    # Only call print_estimate_table_header() here, not before
    # Draw header once before loop
    print_estimate_table_header()
    for item in items:
        if pdf.get_y() > max_y:
            pdf.add_page()
            print_estimate_table_header()
        item_subtotal = Decimal(str(item.get('total', 0)))
        if has_tax:
            item_tax = (item_subtotal * tax_rate / Decimal("100"))
            item_total = item_subtotal + item_tax
            pdf.cell(65, 7, str(item.get('item', ''))[:30], 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(25, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_subtotal:,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_tax:,.0f}", 1, 0, 'R')
            pdf.cell(30, 7, f"Rs {item_total:,.0f}", 1, 1, 'R')
        else:
            pdf.cell(80, 7, str(item.get('item', ''))[:35], 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(30, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(60, 7, f"Rs {item_subtotal:,.0f}", 1, 1, 'R')
    
    # Totals
    pdf.ln(5)
    pdf.set_font("Arial", "", 10)
    pdf.set_x(130)
    pdf.cell(30, 6, "Subtotal:", 0, 0, 'R')
    pdf.cell(30, 6, f"Rs {Decimal(str(estimate_data.get('Subtotal', 0))):,.2f}", 0, 1, 'R')
    
    tax = Decimal(str(estimate_data.get('Tax', 0)))
    if tax > 0:
        pdf.set_x(130)
        pdf.cell(30, 6, "Tax:", 0, 0, 'R')
        pdf.cell(30, 6, f"Rs {tax:,.2f}", 0, 1, 'R')
    
    pdf.ln(2)
    pdf.set_font("Arial", "B", 12)
    pdf.set_x(130)
    pdf.cell(30, 8, "TOTAL:", 0, 0, 'R')
    pdf.cell(30, 8, f"Rs {Decimal(str(estimate_data.get('Total', 0))):,.2f}", 0, 1, 'R')
    
    # Notes
    pdf.set_xy(10, 200)
    pdf.set_font("Arial", "I", 9)
    pdf.multi_cell(0, 5, "This estimate is valid for 30 days from the date issued.")
    
    # Software-generated notice at bottom
    pdf.set_auto_page_break(False)
    pdf.set_y(275)
    pdf.set_font("Arial", "I", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 5, "This is a computer-generated estimate. Please review and approve.", 0, 'C')
    pdf.set_text_color(0, 0, 0)
    
    # Return as BytesIO
    pdf_output = BytesIO()
    pdf_output.write(pdf.output(dest='S').encode('latin1'))
    pdf_output.seek(0)
    return pdf_output


def generate_delivery_chalan_pdf(invoice_data, items, company_info=None):
    """
    Generate a professional Delivery Chalan PDF in memory.
    
    Chalan includes: Sr. No., Item Name, Quantity only (no prices).
    Returns BytesIO object containing the PDF.
    """
    pdf = FPDF()
    pdf.add_page()
    
    # Page border
    pdf.set_draw_color(0, 0, 0)
    pdf.rect(5, 5, 200, 287)
    
    logo_path = get_company_logo_path(get_active_company_name())
    if logo_path.exists():
        pdf.image(str(logo_path), x=10, y=8, w=50)
    
    pdf.set_font("Arial", "B", 16)
    pdf.set_xy(10, 45)
    pdf.cell(0, 8, get_active_company_name(), 0, 1)
    
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 5, "Basement No.11", 0, 1)
    pdf.cell(0, 5, "Office 13B Khyber Plaza, Fazal e Haq Road", 0, 1)
    pdf.cell(0, 5, "Blue Area, Islamabad.", 0, 1)
    pdf.cell(0, 5, f"NTN No: {NTN_NUMBER}", 0, 1)
    if company_info:
        pdf.cell(0, 5, company_info.get("address", ""), 0, 1)
        pdf.cell(0, 5, company_info.get("phone", ""), 0, 1)
    
    pdf.set_font("Arial", "B", 24)
    pdf.set_xy(140, 10)
    pdf.cell(60, 10, "DELIVERY CHALAN", 0, 1, 'R')
    
    pdf.set_font("Arial", "", 10)
    pdf.set_xy(140, 25)
    pdf.cell(60, 5, f"Chalan #: {invoice_data.get('InvoiceID', '')}", 0, 1, 'R')
    pdf.set_xy(140, 30)
    pdf.cell(60, 5, f"Date: {invoice_data.get('Date', '')}", 0, 1, 'R')
    
    pdf.set_xy(10, 75)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 6, "DELIVERED TO:", 0, 1)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 5, invoice_data.get('Customer', invoice_data.get('CustomerName', '')), 0, 1)
    pdf.ln(5)
    
    pdf.set_xy(10, 100)
    pdf.set_font("Arial", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    
    pdf.cell(20, 8, "Sr. No.", 1, 0, 'C', True)
    pdf.cell(120, 8, "Item", 1, 0, 'L', True)
    pdf.cell(40, 8, "Quantity", 1, 1, 'C', True)
    
    pdf.set_font("Arial", "", 10)
    sr_no = 1
    for item in items:
        pdf.cell(20, 7, str(sr_no), 1, 0, 'C')
        pdf.cell(120, 7, str(item.get('item', '')), 1, 0, 'L')
        pdf.cell(40, 7, f"{item.get('qty', '')} units", 1, 1, 'C')
        sr_no += 1
    
    pdf.ln(10)
    pdf.set_font("Arial", "", 9)
    pdf.set_xy(10, 190)
    pdf.multi_cell(0, 5, "This is a delivery chalan for the above-mentioned items. Please verify receipt and sign below.", 0, 'L')
    
    # Signatures at bottom of page
    pdf.set_auto_page_break(False)
    pdf.set_xy(10, 260)
    pdf.set_font("Arial", "", 10)
    
    pdf.cell(90, 5, "_" * 35, 0, 0, 'L')
    pdf.cell(90, 5, "_" * 35, 0, 1, 'R')
    
    pdf.set_xy(10, 267)
    pdf.cell(90, 5, "Sender Signature", 0, 0, 'L')
    pdf.cell(90, 5, "Receiver Signature", 0, 1, 'R')
    
    pdf_output = BytesIO()
    pdf_output.write(pdf.output(dest='S').encode('latin1'))
    pdf_output.seek(0)
    return pdf_output


def export_report_to_pdf(report_title, data_dict, date_range="", company_name=COMPANY_NAME):
    """
    Export financial report to PDF format with professional corporate formatting.
    
    Args:
        report_title: Title of the report
        data_dict: Dictionary containing report data sections with structure:
                   {"section_name": [(label, amount, indent_level, is_bold), ...]}
        date_range: Optional date range string
        company_name: Company name for header
    
    Returns BytesIO object.
    """
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    # Page width is 210mm, margins are 15mm each = 180mm available
    page_width = 210 - (2 * 15)
    
    # Formal Header - Centered
    pdf.set_font("Arial", "B", 14)
    pdf.cell(page_width, 8, company_name, 0, 1, 'C')
    
    pdf.set_font("Arial", "B", 12)
    pdf.cell(page_width, 7, report_title, 0, 1, 'C')
    
    if date_range:
        pdf.set_font("Arial", "", 9)
        pdf.cell(page_width, 5, date_range, 0, 1, 'C')
    
    pdf.ln(4)
    
    # Report Body - Professional formatting with proper column widths
    for section_title, section_data in data_dict.items():
        # Check if we need a new page
        if pdf.get_y() > 250:
            pdf.add_page()
            pdf.set_y(15)
        
        # Section header with line
        if section_title:
            pdf.set_font("Arial", "B", 10)
            pdf.cell(page_width, 6, section_title, 0, 1)
            pdf.set_draw_color(100, 100, 100)
            pdf.line(15, pdf.get_y(), 195, pdf.get_y())
            pdf.ln(2)
        
        # Section items
        if isinstance(section_data, list):
            for item in section_data:
                if isinstance(item, dict):
                    # Legacy format support
                    label = item.get('label', '')
                    value = item.get('value', '')
                    indent = 0
                    is_bold = False
                elif isinstance(item, tuple) and len(item) >= 2:
                    # New format: (label, amount, indent_level, is_bold)
                    label = str(item[0])
                    value = str(item[1])
                    indent = item[2] if len(item) > 2 else 0
                    is_bold = item[3] if len(item) > 3 else False
                else:
                    continue
                
                # Apply formatting
                font_style = "B" if is_bold else ""
                font_size = 9 if is_bold else 8.5
                pdf.set_font("Arial", font_style, font_size)
                
                # Calculate column widths
                # Label column: 120mm, Amount column: 60mm
                label_width = page_width - 45
                amount_width = 45
                
                # Truncate label if too long
                max_label_length = 80 if indent == 0 else 100
                if len(label) > max_label_length:
                    label = label[:max_label_length-3] + "..."
                
                # Get current y position for row height calculation
                y_before = pdf.get_y()
                
                # Draw label with indent
                if indent > 0:
                    pdf.cell(indent * 3, 5, "", 0, 0)  # indent spacing
                    pdf.cell(label_width - (indent * 3), 5, label, 0, 0, 'L')
                else:
                    pdf.cell(label_width, 5, label, 0, 0, 'L')
                
                # Draw amount right-aligned
                pdf.cell(amount_width, 5, value, 0, 1, 'R')
    
    # Footer with page number
    pdf.set_y(-15)
    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(128, 128, 128)
    pdf.cell(page_width, 5, f"Page {pdf.page_no()}", 0, 0, 'C')
    
    pdf_output = BytesIO()
    pdf_output.write(pdf.output(dest='S').encode('latin1'))
    pdf_output.seek(0)
    return pdf_output


def export_account_ledger_to_pdf(account_number, account_name, start_date, end_date, ledger_entries, opening_balance, closing_balance, company_name=COMPANY_NAME):
    """
    Export account ledger to PDF in proper table format.
    
    Args:
        account_number: Account number
        account_name: Account name
        start_date: Start date string
        end_date: End date string
        ledger_entries: List of ledger entries
        opening_balance: Opening balance value
        closing_balance: Closing balance value
        company_name: Company name
    
    Returns BytesIO object.
    """
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    # Define has_tax for table rendering
    has_tax = False  # Default: no tax columns
    tax_rate = Decimal("0")
    # If ledger_entries contain tax info, update has_tax and tax_rate
    if ledger_entries and isinstance(ledger_entries, list):
        for entry in ledger_entries:
            if isinstance(entry, dict) and entry.get('TaxType') == 'With Tax':
                has_tax = True
                # Try to get tax rate from entry if available
                if 'TaxRate' in entry:
                    try:
                        tax_rate = Decimal(str(entry['TaxRate']))
                    except Exception:
                        tax_rate = Decimal("0")
                break
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    # Page dimensions
    page_width = 210 - (2 * 15)  # 180mm usable width
    
    # Header
    pdf.set_font("Arial", "B", 14)
    pdf.cell(page_width, 8, company_name, 0, 1, 'C')
    
    pdf.set_font("Arial", "B", 12)
    pdf.cell(page_width, 7, "ACCOUNT LEDGER", 0, 1, 'C')
    
    pdf.set_font("Arial", "", 9)
    pdf.cell(page_width, 5, f"Account: {account_number} - {account_name}", 0, 1, 'C')
    pdf.cell(page_width, 5, f"Period: {start_date} to {end_date}", 0, 1, 'C')
    
    pdf.ln(3)
    
    # Table Rows with page break and header repeat
    pdf.set_font("Arial", "", 10)
    max_y = 265
    # Define column widths for ledger table
    date_width = 25
    description_width = 65
    debit_width = 25
    credit_width = 25
    balance_width = 40
    def print_po_table_header():
        pdf.set_font("Arial", "B", 10)
        pdf.set_fill_color(240, 240, 240)
        if has_tax:
            pdf.cell(65, 8, "Item", 1, 0, 'L', True)
            pdf.cell(20, 8, "Qty", 1, 0, 'C', True)
            pdf.cell(25, 8, "Unit Price", 1, 0, 'R', True)
            pdf.cell(25, 8, "Subtotal", 1, 0, 'R', True)
            pdf.cell(25, 8, "Tax", 1, 0, 'R', True)
            pdf.cell(30, 8, "Total", 1, 1, 'R', True)
        else:
            pdf.cell(80, 8, "Item", 1, 0, 'L', True)
            pdf.cell(20, 8, "Qty", 1, 0, 'C', True)
            pdf.cell(30, 8, "Unit Price", 1, 0, 'R', True)
            pdf.cell(60, 8, "Total", 1, 1, 'R', True)
        pdf.set_font("Arial", "", 10)
    print_po_table_header()
    # Use ledger_entries as items for table
    items = ledger_entries if isinstance(ledger_entries, list) else []
    for item in items:
        if pdf.get_y() > max_y:
            pdf.add_page()
            print_po_table_header()
        item_subtotal = Decimal(str(item.get('total', 0)))
        if has_tax:
            item_tax = (item_subtotal * tax_rate / Decimal("100"))
            item_total = item_subtotal + item_tax
            pdf.cell(65, 7, str(item.get('item', ''))[:30], 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(25, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_subtotal:,.0f}", 1, 0, 'R')
            pdf.cell(25, 7, f"Rs {item_tax:,.0f}", 1, 0, 'R')
            pdf.cell(30, 7, f"Rs {item_total:,.0f}", 1, 1, 'R')
        else:
            pdf.cell(80, 7, str(item.get('item', ''))[:35], 1, 0, 'L')
            pdf.cell(20, 7, str(item.get('qty', '')), 1, 0, 'C')
            pdf.cell(30, 7, f"Rs {Decimal(str(item.get('price', 0))):,.0f}", 1, 0, 'R')
            pdf.cell(60, 7, f"Rs {item_subtotal:,.0f}", 1, 1, 'R')
    # Table Body
    pdf.set_font("Arial", "", 8.5)
    pdf.set_fill_color(255, 255, 255)
    
    running_balance = Decimal(str(opening_balance))
    
    # Opening Balance Row
    pdf.set_fill_color(245, 245, 245)  # Light gray for opening balance
    pdf.cell(date_width, 6, "", 1, 0)  # Empty date cell
    pdf.cell(description_width, 6, "Opening Balance", 1, 0)
    pdf.cell(debit_width, 6, "", 1, 0, 'R')
    pdf.cell(credit_width, 6, "", 1, 0, 'R')
    pdf.cell(balance_width, 6, f"{running_balance:,.2f}", 1, 1, 'R')
    
    # Data Rows - only show if there are transactions
    pdf.set_fill_color(255, 255, 255)
    has_transactions = len(ledger_entries) > 1  # More than just opening balance
    
    if has_transactions:
        for entry in ledger_entries[1:]:  # Skip opening balance row already shown
            # Check if we need a new page
            if pdf.get_y() > 250:
                pdf.add_page()
                pdf.set_y(15)
                
                # Repeat header on new page
                pdf.set_font("Arial", "B", 9)
                pdf.set_fill_color(200, 200, 200)
                pdf.cell(date_width, 7, "Date", 1, 0, 'C', True)
                pdf.cell(description_width, 7, "Description", 1, 0, 'C', True)
                pdf.cell(debit_width, 7, "Debit", 1, 0, 'C', True)
                pdf.cell(credit_width, 7, "Credit", 1, 0, 'C', True)
                pdf.cell(balance_width, 7, "Balance", 1, 1, 'C', True)
                pdf.set_font("Arial", "", 8.5)
                pdf.set_fill_color(255, 255, 255)
            
            date_str = entry['Date']
            description = entry['Description'][:35]  # Truncate long descriptions
            
            # Safely convert debit/credit to Decimal
            try:
                # Handle both Decimal objects and formatted strings
                debit_val = entry['Debit']
                credit_val = entry['Credit']
                
                if isinstance(debit_val, str):
                    # Extract number from "Rs 1,000.00" format
                    if debit_val and debit_val != "":
                        debit_val = Decimal(debit_val.replace("Rs ", "").replace(",", ""))
                    else:
                        debit_val = Decimal("0")
                else:
                    debit_val = Decimal(str(debit_val))
                
                if isinstance(credit_val, str):
                    # Extract number from "Rs 1,000.00" format
                    if credit_val and credit_val != "":
                        credit_val = Decimal(credit_val.replace("Rs ", "").replace(",", ""))
                    else:
                        credit_val = Decimal("0")
                else:
                    credit_val = Decimal(str(credit_val))
            except Exception:
                logging.exception("Failed parsing debit/credit for entry: %s", entry)
                debit_val = Decimal("0")
                credit_val = Decimal("0")
            
            debit_str = f"{debit_val:,.2f}" if debit_val > 0 else ""
            credit_str = f"{credit_val:,.2f}" if credit_val > 0 else ""
            
            # Update running balance
            running_balance += debit_val
            running_balance -= credit_val
            
            balance_str = f"{running_balance:,.2f}"
            
            # Draw row
            pdf.cell(date_width, 6, date_str, 1, 0, 'C')
            pdf.cell(description_width, 6, description, 1, 0, 'L')
            pdf.cell(debit_width, 6, debit_str, 1, 0, 'R')
            pdf.cell(credit_width, 6, credit_str, 1, 0, 'R')
            pdf.cell(balance_width, 6, balance_str, 1, 1, 'R')
    
    # Closing Balance Row
    pdf.set_fill_color(245, 245, 245)  # Light gray for closing balance
    pdf.set_font("Arial", "B", 8.5)
    pdf.cell(date_width, 6, "", 1, 0)
    pdf.cell(description_width, 6, "Closing Balance", 1, 0)
    pdf.cell(debit_width, 6, "", 1, 0, 'R')
    pdf.cell(credit_width, 6, "", 1, 0, 'R')
    closing_dec = Decimal(str(closing_balance))
    pdf.cell(balance_width, 6, f"{closing_dec:,.2f}", 1, 1, 'R')
    
    # Footer
    pdf.set_y(-15)
    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(128, 128, 128)
    pdf.cell(page_width, 5, f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Page {pdf.page_no()}", 0, 0, 'C')
    
    # Return as BytesIO
    pdf_output = BytesIO()
    pdf_output.write(pdf.output(dest='S').encode('latin1'))
    pdf_output.seek(0)
    return pdf_output

# ==================== AUTHENTICATION ====================
def login():
    """Handle user login."""
    if st.session_state.get("authenticated"):
        return True
    
    st.title("Login")
    username = st.text_input(t('username'))
    password = st.text_input(t('password'), type="password")
    
    if st.button("Login"):
        with get_db() as conn:
            user = conn.execute("SELECT PasswordHash, Role FROM users WHERE Username = ?", (username,)).fetchone()
        
        if user and verify_password(password, user[0]):
            st.session_state.authenticated = True
            st.session_state.username = username
            st.session_state.role = user[1]
            st.rerun()
        else:
            st.error("Invalid credentials")
    
    return False

def require_admin():
    """Ensure user is admin."""
    if st.session_state.get("role") != "admin":
        st.error("Admin access required")
        st.stop()

# ==================== UI COMPONENTS ====================
def show_dashboard():
    """Dashboard with key metrics in styled cards."""
    # Initialize language in session state if not exists
    if 'language' not in st.session_state:
        st.session_state.language = 'English'
    
    st.markdown(f"""
        <h2 style='text-align: center; color: #D4AF37; font-size: 2.5em; font-family: Metro, Arial, sans-serif; letter-spacing: 2px; text-shadow: 1px 1px 8px #bfa14a;'>{get_active_company_name().upper()}</h2>
    """, unsafe_allow_html=True)
    st.markdown(f"<h3 style='text-align: center; color: #333333;'>{t('business_dashboard')}</h3>", unsafe_allow_html=True)
    
    # Month selector and Language selector
    col1, col2, col3 = st.columns([2, 2, 2])
    with col1:
        selected_month = st.date_input(t('select_month'), value=datetime.now())
    with col3:
        language = st.selectbox(
            f"🌐 {t('language')}",
            options=['English', 'اردو (Urdu)', 'العربية (Arabic)'],
            index=(['English', 'اردو (Urdu)', 'العربية (Arabic)'].index(st.session_state.language)
                   if st.session_state.language in ['English', 'اردو (Urdu)', 'العربية (Arabic)']
                   else 0),
            key='lang_selector'
        )
        if language != st.session_state.language:
            st.session_state.language = language
            st.rerun()
    
    month_start = selected_month.replace(day=1)
    if selected_month.month == 12:
        month_end = selected_month.replace(year=selected_month.year + 1, month=1, day=1)
    else:
        month_end = selected_month.replace(month=selected_month.month + 1, day=1)
    
    month_start_str = month_start.strftime("%Y-%m-%d")
    month_end_str = month_end.strftime("%Y-%m-%d")
    period_text = f"{t('for_the_period')} {month_start.strftime('%d %B %Y')} {t('to')} {(month_end - pd.Timedelta(days=1)).strftime('%d %B %Y')}"
    st.markdown(f"<p style='text-align: center; color: #666666; font-size: 14px;'>{period_text}</p>", unsafe_allow_html=True)
    
    st.divider()
    
    # Load data
    invoices = load_table("invoices")
    bills = load_table("bills")
    expenses = load_table("expenses")
    
    # Filter by month
    invoices_month = invoices[(invoices['Date'] >= month_start_str) & (invoices['Date'] < month_end_str)]
    bills_month = bills[(bills['Date'] >= month_start_str) & (bills['Date'] < month_end_str)]
    expenses_month = expenses[(expenses['Date'] >= month_start_str) & (expenses['Date'] < month_end_str)]
    
    # Calculate totals - only count POSTED invoices and bills
    total_sales = Decimal("0.00")
    posted_invoices_month = invoices_month[invoices_month['Status'].isin(['Posted', 'Paid'])]
    for _, inv in posted_invoices_month.iterrows():
        total_sales += to_decimal_safe(inv['Total'])
    
    total_purchases = Decimal("0.00")
    posted_bills_month = bills_month[bills_month['Status'].isin(['Posted', 'Paid'])]
    for _, bill in posted_bills_month.iterrows():
        total_purchases += to_decimal_safe(bill['Total'])
    
    total_expenses = Decimal("0.00")
    for _, exp in expenses_month.iterrows():
        total_expenses += to_decimal_safe(exp['Amount'])
    
    # Calculate actual COGS for posted invoices to get accurate profit
    cogs = calculate_cogs_from_invoices(posted_invoices_month) if not posted_invoices_month.empty else Decimal("0.00")
    
    # Net Profit = Sales - COGS - Expenses (purchases are already in COGS)
    net_profit = total_sales - cogs - total_expenses
    
    # Display metrics in styled cards with color coding
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #10b981 0%, #059669 100%); 
                    padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
            <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='{t('total_sales')}'>💰 {t('total_sales')}</p>
            <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {total_sales:,.2f}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); 
                    padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
            <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='{t('cogs')}'>📦 {t('cogs')}</p>
            <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {cogs:,.2f}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); 
                    padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
            <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='{t('total_expenses')}'>💸 {t('total_expenses')}</p>
            <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {total_expenses:,.2f}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        profit_color = '#10b981' if net_profit >= 0 else '#ef4444'
        profit_gradient = 'linear-gradient(135deg, #10b981 0%, #059669 100%)' if net_profit >= 0 else 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)'
        profit_icon = '📈' if net_profit >= 0 else '📉'
        st.markdown(f"""
        <div style='background: {profit_gradient}; 
                    padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
            <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='{t('net_profit')}'>{profit_icon} {t('net_profit')}</p>
            <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {net_profit:,.2f}</h2>
        </div>
        """, unsafe_allow_html=True)
    
    # Loan summary cards - calculate from current balances in loan_parties table
    st.divider()
    
    try:
        loan_parties = load_table("loan_parties")
    except Exception:
        loan_parties = pd.DataFrame()
    
    # Calculate actual outstanding loan amounts from balances
    total_loan_received = Decimal("0.00")  # We owe them (positive balances)
    total_loan_given = Decimal("0.00")     # They owe us (negative balances)
    
    if not loan_parties.empty:
        for _, party in loan_parties.iterrows():
            balance = to_decimal_safe(party.get('Balance', 0))
            if balance > 0:
                total_loan_received += balance  # We received loan, we owe them
            elif balance < 0:
                total_loan_given += abs(balance)  # We gave loan, they owe us

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #06b6d4 0%, #0284c7 100%); 
                    padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
            <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0;'>{t('loans_received')}</p>
            <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {total_loan_received:,.2f}</h2>
        </div>
        """, unsafe_allow_html=True)
    with col_b:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #f97316 0%, #ea580c 100%); 
                    padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
            <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0;'>{t('loans_given')}</p>
            <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {total_loan_given:,.2f}</h2>
        </div>
        """, unsafe_allow_html=True)

    st.divider()
    
    # Additional Inventory Metrics
    inventory = load_table("inventory")
    if not inventory.empty:
        # Calculate total cost price of all stock
        total_stock_value = Decimal("0.00")
        for _, item in inventory.iterrows():
            qty = to_decimal_safe(item['Quantity'], Decimal("0"))
            cost = to_decimal_safe(item['CostPrice'], Decimal("0.00"))
            total_stock_value += qty * cost
        

        # Compute total open balances separately for customers and vendors
        customers = load_table("customers")
        vendors = load_table("vendors")
        total_customer_open = Decimal("0.00")
        total_vendor_open = Decimal("0.00")
        if not customers.empty:
            for _, c in customers.iterrows():
                total_customer_open += to_decimal_safe(c.get('OpenBalance', 0))
        if not vendors.empty:
            for _, v in vendors.iterrows():
                total_vendor_open += to_decimal_safe(v.get('OpenBalance', 0))

        col1, col2, col3 = st.columns([1, 1, 1])

        with col1:
            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); 
                        padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
                <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0;'>{t('total_stock_value')}</p>
                <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {total_stock_value:,.2f}</h2>
            </div>
            """, unsafe_allow_html=True)

        with col2:
            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #06b6d4 0%, #0284c7 100%); 
                        padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
                <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0;'>{t('total_customer_balance')}</p>
                <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {total_customer_open:,.2f}</h2>
            </div>
            """, unsafe_allow_html=True)

        with col3:
            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #f97316 0%, #ea580c 100%); 
                        padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); min-height: 100px;'>
                <p style='color: rgba(255,255,255,0.9); font-size: 14px; margin: 0;'>{t('total_vendor_balance')}</p>
                <h2 style='color: white; margin: 8px 0 0 0; font-size: 24px;'>Rs {total_vendor_open:,.2f}</h2>
            </div>
            """, unsafe_allow_html=True)

        st.divider()
        
        low_stock = inventory[inventory["Quantity"] <= inventory["ReorderLevel"]]
        
        if not low_stock.empty:
            st.warning(f"⚠️ **Low Stock Alert:** {len(low_stock)} item(s) below reorder level")
            
            with st.expander("📋 View Low Stock Items", expanded=False):
                display_df = low_stock[["ItemName", "Quantity", "ReorderLevel"]].copy()
                display_df.columns = ["Item", "Current Qty", "Reorder Level"]
                st.dataframe(display_df, hide_index=True, width="stretch")
                
                if st.button("🔄 Go to Inventory", key="goto_inventory_from_dashboard", width="stretch"):
                    st.session_state.nav_choice = "Stock List"
                    st.rerun()

def show_manage_invoices_section():
    """Standalone Manage Invoices section."""
    st.markdown("### 📄 Manage Invoices")
    
    try:
        invoices = load_table("invoices")
        customers = load_table("customers")
        
        if invoices.empty:
            st.info("📭 No invoices found. Create estimates and convert them to invoices to get started.")
            return
        
        # Filters
        col1, col2, col3 = st.columns([2, 2, 2])
        
        with col1:
            status_filter = st.selectbox(t('status_label'), [t('all'), t('draft'), t('posted')], key="inv_status_filter")
        
        with col2:
            # Date range filter with defaults
            from_date = st.date_input(t('from_date_label'), 
                value=datetime.now().date().replace(day=1),
                key="inv_from_date")
        
        with col3:
            to_date = st.date_input(t('to_date_label'), 
                value=datetime.now().date(),
                key="inv_to_date")
        
        # Apply filters
        filtered_invoices = invoices.copy()
        
        # Status filter
        if status_filter != "All":
            filtered_invoices = filtered_invoices[filtered_invoices["Status"] == status_filter]
        
        # Date filter
        filtered_invoices["DateParsed"] = pd.to_datetime(filtered_invoices["Date"], errors='coerce')
        from_datetime = pd.to_datetime(from_date)
        to_datetime = pd.to_datetime(to_date) + pd.Timedelta(days=1)  # Include end date
        filtered_invoices = filtered_invoices[
            (filtered_invoices["DateParsed"] >= from_datetime) & 
            (filtered_invoices["DateParsed"] < to_datetime)
        ]
        
        # Show count
        st.caption(f"📊 Showing {len(filtered_invoices)} of {len(invoices)} invoice(s)")
        
        if filtered_invoices.empty:
            st.warning("🔍 No invoices found for selected filters.")
        else:
            for _, inv in filtered_invoices.iloc[::-1].iterrows():
                with st.expander(f"{inv['InvoiceID']} - Rs {to_decimal_safe(inv['Total']):,.2f} ({inv['Status']})"):
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Date", inv['Date'])
                    col2.metric("Subtotal", f"Rs {to_decimal_safe(inv['Subtotal']):,.2f}")
                    col3.metric("Total", f"Rs {to_decimal_safe(inv['Total']):,.2f}")
                    
                    st.write("**Items:**")
                    items = get_invoice_line_items(inv['InvoiceID'])
                    if items:
                        items_df = pd.DataFrame(items)
                        # Keep only display columns (drop item_id if present)
                        display_cols = ['item', 'qty', 'price', 'total']
                        items_df = items_df[[col for col in display_cols if col in items_df.columns]]
                        items_df.insert(0, 'Sr. No.', range(1, len(items_df) + 1))
                        items_df.columns = ['Sr. No.', 'Item', 'Qty', 'Price', 'Total']
                        st.dataframe(items_df, hide_index=True, width="stretch")
                    else:
                        st.write("No items")
                    
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        inv_id = str(inv.get("InvoiceID", ""))
                        cust_name = ""
                        try:
                            cust_name = customers[customers["CustomerID"] == inv["CustomerID"]].iloc[0]["CustomerName"]
                        except Exception:
                            cust_name = ""

                        def _render_download_menu():
                            inv_data = inv.to_dict()
                            inv_data["Customer"] = cust_name
                            items_json = inv.get('ItemsJSON', '[]')
                            items = parse_items_json(items_json)

                            invoice_bytes = _pdf_to_bytes(generate_professional_invoice_pdf(inv_data, items))
                            chalan_bytes = _pdf_to_bytes(generate_delivery_chalan_pdf(inv_data, items))

                            st.download_button(
                                "⬇️ Invoice",
                                BytesIO(invoice_bytes),
                                file_name=f"{inv_id}.pdf",
                                mime="application/pdf",
                                key=f"download_invoice_{inv_id}",
                                width="stretch",
                            )
                            st.download_button(
                                "⬇️ Delivery Chalan",
                                BytesIO(chalan_bytes),
                                file_name=f"{inv_id}_chalan.pdf",
                                mime="application/pdf",
                                key=f"download_chalan_{inv_id}",
                                width="stretch",
                            )

                        if hasattr(st, "popover"):
                            with st.popover("📥 Download", use_container_width=True):
                                _render_download_menu()
                        else:
                            with st.expander("📥 Download", expanded=False):
                                _render_download_menu()
                    
                    with col2:
                        inv_id = str(inv.get("InvoiceID", ""))
                        cust_name = ""
                        try:
                            cust_name = customers[customers["CustomerID"] == inv["CustomerID"]].iloc[0]["CustomerName"]
                        except Exception:
                            cust_name = ""

                        def _render_share_menu():
                            doc_choice = st.radio(
                                "Share what?",
                                options=["Invoice", "Delivery Chalan"],
                                horizontal=True,
                                key=f"share_doc_{inv_id}",
                            )
                            channel = st.radio(
                                "Share via",
                                options=[t('whatsapp'), t('email')],
                                horizontal=True,
                                key=f"share_channel_{inv_id}",
                            )

                            doc_type = "Invoice" if doc_choice == "Invoice" else "Delivery Chalan"
                            customer_part = f" for {cust_name}" if cust_name else ""
                            date_part = f" dated {str(inv.get('Date', ''))}" if str(inv.get('Date', '')).strip() else ""
                            message = f"{doc_type} {inv_id}{customer_part}{date_part}. Please find the PDF attached."

                            wa_url = "https://api.whatsapp.com/send?text=" + quote(message)
                            subject = f"{doc_type} {inv_id}"
                            body = message + "\n\n(Download the PDF from the app and attach it here.)"
                            mailto_url = "mailto:?subject=" + quote(subject) + "&body=" + quote(body)

                            if channel == t('whatsapp'):
                                if hasattr(st, "link_button"):
                                    st.link_button(f"🟢 {t('whatsapp')}", wa_url, use_container_width=True)
                                else:
                                    st.markdown(f"[🟢 {t('whatsapp')}]({wa_url})")
                            else:
                                if hasattr(st, "link_button"):
                                    st.link_button(f"✉️ {t('email')}", mailto_url, use_container_width=True)
                                else:
                                    st.markdown(f"[✉️ {t('email')}]({mailto_url})")

                        if hasattr(st, "popover"):
                            with st.popover("📤 Share", use_container_width=True):
                                _render_share_menu()
                        else:
                            with st.expander("📤 Share", expanded=False):
                                _render_share_menu()
                    
                    with col3:
                        if inv["Status"] == "Draft":
                            if st.button("✅ Post Invoice", key=f"post_{inv['InvoiceID']}", type="primary"):
                                try:
                                    # Read items from relational table (with JSON fallback)
                                    items = get_invoice_line_items(inv['InvoiceID'])
                                    inventory = load_table("inventory")
                                    
                                    # Check stock availability first
                                    can_post = True
                                    errors = []
                                    stock_updates = []  # Track updates for transaction
                                    
                                    for item in items:
                                        item_id = item.get('item_id') or item.get('ItemID')
                                        if item_id:
                                            item_row = inventory[inventory["ItemID"] == item_id]
                                        else:
                                            item_name = item.get('item') or item.get('name')
                                            item_row = inventory[inventory["ItemName"] == item_name]

                                        if not item_row.empty:
                                            current_qty = int(item_row.iloc[0]["Quantity"])
                                            required_qty = int(item.get("qty", 0))
                                            if current_qty < required_qty:
                                                can_post = False
                                                display_name = item_row.iloc[0]["ItemName"]
                                                errors.append(f"{display_name}: Available {current_qty}, Required {required_qty}")
                                            else:
                                                stock_updates.append((item_id or item_name, item_id is not None, required_qty))
                                    
                                    if not can_post:
                                        st.error("❌ Insufficient stock:\n" + "\n".join(errors))
                                    else:
                                        # Atomic transaction: stock deduction + balance + status update
                                        with get_db() as conn:
                                            try:
                                                # Deduct stock with direct SQL for atomicity
                                                for identifier, is_id, qty in stock_updates:
                                                    if is_id:
                                                        conn.execute("UPDATE inventory SET Quantity = Quantity - ? WHERE ItemID = ?", (qty, identifier))
                                                    else:
                                                        conn.execute("UPDATE inventory SET Quantity = Quantity - ? WHERE ItemName = ?", (qty, identifier))
                                                
                                                # Update customer balance with precise Decimal arithmetic
                                                update_balance_precise(conn, "customers", "CustomerID", inv["CustomerID"], to_decimal_safe(inv['Total']), "add")
                                                
                                                # Mark invoice as posted
                                                conn.execute(
                                                    "UPDATE invoices SET Status = 'Posted' WHERE InvoiceID = ?",
                                                    (inv["InvoiceID"],)
                                                )
                                                
                                                conn.commit()
                                                st.success("✅ Posted and stock deducted")
                                                st.rerun()
                                            except Exception as e:
                                                conn.rollback()
                                                raise e
                                except Exception as e:
                                    st.error(f"Error posting invoice: {e}")
                                    logging.exception("Invoice posting failed for %s", inv.get('InvoiceID'))
                        
                        # We intentionally do not support marking invoices "Paid".
                        # Customers often send partial/unallocated payments; balances are managed via Payments.
    
    except Exception as e:
        st.error(f"Error loading invoices: {str(e)}")
        st.exception(e)

def show_sales():
    """Sales section - uses tabs for sub-pages."""
    st.title(f"💰 {t('sales')}")
    
    # Show tabs for navigation
    tab1, tab2, tab3, tab4 = st.tabs([t('create_estimate'), t('create_invoice'), t('convert_to_invoice'), t('manage_invoices')])
    
    with tab1:
        st.markdown(f"### 📋 {t('create_estimate')}")
        
        # Initialize session state first (before any conditional checks)
        if 'estimate_items' not in st.session_state:
            st.session_state.estimate_items = []
        if 'est_tax_type' not in st.session_state:
            st.session_state.est_tax_type = "Without Tax"
        if 'est_tax_rate' not in st.session_state:
            st.session_state.est_tax_rate = 18.0
        
        customers = load_table("customers")
        inventory = load_table("inventory")
        
        if customers.empty:
            st.warning(f"⚠️ {t('create')} {t('customer')} {t('first')}" if t('first') != 'first' else "⚠️ Create a customer first")
        elif inventory.empty:
            st.warning(f"⚠️ {t('add')} {t('item')} {t('first')}" if t('first') != 'first' else "⚠️ Add items to inventory first")
        else:
            
            # Customer Selection
            selected_customer = st.selectbox(f"👤 {t('select_customer')}", 
                [f"{c['CustomerID']} - {c['CustomerName']}" for _, c in customers.iterrows()],
                key="est_customer")
            customer_id = selected_customer.split(" - ")[0]
            
            st.divider()
            
            # Add Items Section
            st.markdown("#### 🛒 Add Items")
            
            with st.form("add_items_form", clear_on_submit=True):
                selected_items = st.multiselect("Select Item(s)", 
                    inventory["ItemName"].tolist(),
                    help="You can select multiple items at once")
                
                # Show quantity inputs for each selected item
                item_quantities = {}
                if selected_items:
                    st.markdown("**Set quantities for each item:**")
                    cols_per_row = 3
                    for i in range(0, len(selected_items), cols_per_row):
                        cols = st.columns(cols_per_row)
                        for j, col in enumerate(cols):
                            idx = i + j
                            if idx < len(selected_items):
                                item_name = selected_items[idx]
                                with col:
                                    qty = st.number_input(
                                        f"{item_name}",
                                        min_value=1,
                                        max_value=10000,
                                        value=1,
                                        key=f"qty_{item_name}_{idx}",
                                        help=f"Quantity for {item_name}"
                                    )
                                    item_quantities[item_name] = qty
                
                add_button = st.form_submit_button("➕ Add Items", type="primary", width="stretch")
            
            if add_button and selected_items:
                for selected_item in selected_items:
                    item_data = inventory[inventory["ItemName"] == selected_item].iloc[0]
                    custom_price = get_customer_price(customer_id, item_data["ItemID"])
                    price = to_decimal_safe(custom_price)
                    qty = item_quantities.get(selected_item, 1)
                    total = qty * price
                    
                    # Check if item already exists
                    existing_item = next((item for item in st.session_state.estimate_items if item['item'] == selected_item), None)
                    if existing_item:
                        existing_item['qty'] += qty
                        existing_item['total'] = existing_item['qty'] * existing_item['price']
                    else:
                        st.session_state.estimate_items.append({
                            "item": selected_item,
                            "qty": qty,
                            "price": price,
                            "total": total
                        })
                st.success(f"✅ Added {len(selected_items)} item(s)")
                st.rerun()
            
            # Tax & Discount section (moved before items table to show tax in table)
            st.divider()
            st.markdown("#### 💰 Tax & Discount")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            tax_type = st.selectbox(t('tax_type'), [t('without_tax'), t('with_tax')], 
                index=0 if st.session_state.est_tax_type == "Without Tax" else 1,
                key="tax_type_select")
            st.session_state.est_tax_type = tax_type
        with col2:
            tax_rate = st.number_input(t('tax_rate_percent'), 0.0, 100.0, st.session_state.est_tax_rate, 
                step=0.5, disabled=(tax_type == t('without_tax')))
            st.session_state.est_tax_rate = tax_rate if tax_type == t('with_tax') else 0.0
        with col3:
            discount_input = st.number_input(t('discount_rs'), 0.0, step=10.0, key="est_discount")
        
        st.divider()
        
        # Display added items with tax
        if st.session_state.estimate_items:
            st.markdown("#### 📦 Items in Estimate")
            
            # Table header
            col1, col2, col3, col4, col5, col6, col7 = st.columns([2.5, 0.8, 0.6, 0.8, 0.8, 1, 0.4])
            col1.markdown("**Item**")
            col2.markdown("**Price**")
            col3.markdown("**Qty**")
            col4.markdown("**Subtotal**")
            col5.markdown("**Tax**")
            col6.markdown("**Total**")
            col7.markdown("")
            
            for idx, item in enumerate(st.session_state.estimate_items):
                item_subtotal = Decimal(str(item['total']))
                item_tax = (item_subtotal * Decimal(str(st.session_state.est_tax_rate)) / Decimal("100")) if tax_type == "With Tax" else Decimal("0.00")
                item_total_with_tax = item_subtotal + item_tax
                
                col1, col2, col3, col4, col5, col6, col7 = st.columns([2.5, 0.8, 0.6, 0.8, 0.8, 1, 0.4])
                with col1:
                    st.write(item['item'])
                with col2:
                    st.write(f"Rs {item['price']:,.0f}")
                with col3:
                    new_qty = st.number_input("", 1, 10000, item['qty'], key=f"qty_{idx}", label_visibility="collapsed")
                    if new_qty != item['qty']:
                        st.session_state.estimate_items[idx]['qty'] = new_qty
                        st.session_state.estimate_items[idx]['total'] = new_qty * item['price']
                        st.rerun()
                with col4:
                    st.write(f"Rs {item_subtotal:,.0f}")
                with col5:
                    if tax_type == "With Tax":
                        st.write(f"Rs {item_tax:,.0f}")
                    else:
                        st.write("Rs 0")
                with col6:
                    st.write(f"**Rs {item_total_with_tax:,.0f}**")
                with col7:
                    if st.button("🗑️", key=f"del_{idx}", help="Remove"):
                        st.session_state.estimate_items.pop(idx)
                        st.rerun()
            
            st.divider()
            
            # Calculate totals
            subtotal = sum(Decimal(str(item['total'])) for item in st.session_state.estimate_items)
            total_tax = (subtotal * Decimal(str(st.session_state.est_tax_rate)) / Decimal("100")) if tax_type == "With Tax" else Decimal("0.00")
            grand_total = subtotal + total_tax - Decimal(str(discount_input))
            
            # Summary
            st.markdown("#### 📊 Summary")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Subtotal", f"Rs {subtotal:,.2f}")
            col2.metric("Tax ({:.1f}%)".format(st.session_state.est_tax_rate if tax_type == "With Tax" else 0), f"Rs {total_tax:,.2f}")
            col3.metric("Discount", f"Rs {Decimal(str(discount_input)):,.2f}")
            col4.metric("Grand Total", f"Rs {grand_total:,.2f}")
            
            # Final form for submission
            with st.form("create_estimate_final"):
                notes = st.text_area("📝 Notes (Optional)", placeholder="Add any special terms or conditions...")
                
                col1, col2 = st.columns(2)
                with col1:
                    submit = st.form_submit_button("✅ Create Estimate", type="primary", width="stretch")
                with col2:
                    clear = st.form_submit_button("🗑️ Clear All", width="stretch")
                
                if submit:
                    estimate_id = get_next_id("EST", "estimates")
                    customer_name = customers[customers["CustomerID"] == customer_id].iloc[0]["CustomerName"]
                    
                    estimate = {
                        "EstimateID": estimate_id,
                        "Date": datetime.now().strftime("%Y-%m-%d"),
                        "CustomerID": customer_id,
                        "ItemsJSON": json.dumps(st.session_state.estimate_items, default=str),
                        "Subtotal": str(subtotal),
                        "TaxType": tax_type,
                        "TaxRate": str(st.session_state.est_tax_rate),
                        "Tax": str(total_tax),
                        "Discount": str(discount_input),
                        "Total": str(grand_total),
                        "Status": "Draft",
                        "Notes": notes
                    }
                    
                    estimates = load_table("estimates")
                    estimates = pd.concat([estimates, pd.DataFrame([estimate])], ignore_index=True)
                    save_table("estimates", estimates)
                    
                    # Dual-write: Save line items to relational table
                    save_estimate_line_items(estimate_id, st.session_state.estimate_items)
                    
                    st.success(f"✅ Estimate {estimate_id} created for {customer_name}!")
                    st.session_state.estimate_items = []
                    st.session_state.est_tax_type = "Without Tax"
                    st.session_state.est_tax_rate = 18.0
                    st.rerun()
                
                if clear:
                    st.session_state.estimate_items = []
                    st.session_state.est_tax_type = "Without Tax"
                    st.session_state.est_tax_rate = 18.0
                    st.rerun()
        else:
            st.info("👆 Select one or more items above to start creating the estimate")
    
    with tab2:
        st.markdown(f"### 🧾 {t('create_invoice')}")
        # Direct invoice creation (bypasses estimate)
        # Initialize session state for invoice items
        if 'invoice_items' not in st.session_state:
            st.session_state.invoice_items = []
        if 'inv_tax_type' not in st.session_state:
            st.session_state.inv_tax_type = "Without Tax"
        if 'inv_tax_rate' not in st.session_state:
            st.session_state.inv_tax_rate = 18.0

        customers = load_table("customers")
        inventory = load_table("inventory")

        if customers.empty:
            st.warning(f"⚠️ {t('create')} {t('customer')} {t('first')}")
        elif inventory.empty:
            st.warning(f"⚠️ {t('add')} {t('item')} {t('first')}")
        else:
            selected_customer = st.selectbox(f"👤 {t('select_customer')}", 
                [f"{c['CustomerID']} - {c['CustomerName']}" for _, c in customers.iterrows()],
                key="inv_customer")
            customer_id = selected_customer.split(" - ")[0]

            st.divider()
            st.markdown("#### 🛒 Add Items")
            with st.form("add_invoice_items", clear_on_submit=True):
                selected_items = st.multiselect("Select Item(s)", inventory["ItemName"].tolist())
                item_quantities = {}
                if selected_items:
                    st.markdown("**Set quantities for each item:**")
                    cols_per_row = 3
                    for i in range(0, len(selected_items), cols_per_row):
                        cols = st.columns(cols_per_row)
                        for j, col in enumerate(cols):
                            idx = i + j
                            if idx < len(selected_items):
                                item_name = selected_items[idx]
                                with col:
                                    qty = st.number_input(f"{item_name}", min_value=1, max_value=10000, value=1, key=f"inv_qty_{item_name}_{idx}")
                                    item_quantities[item_name] = qty
                add_inv = st.form_submit_button("➕ Add Items", type="primary")

            if add_inv and selected_items:
                for selected_item in selected_items:
                    item_data = inventory[inventory["ItemName"] == selected_item].iloc[0]
                    custom_price = get_customer_price(customer_id, item_data["ItemID"])
                    price = to_decimal_safe(custom_price)
                    qty = item_quantities.get(selected_item, 1)
                    total = qty * price
                    existing_item = next((item for item in st.session_state.invoice_items if item['item'] == selected_item), None)
                    if existing_item:
                        existing_item['qty'] += qty
                        existing_item['total'] = existing_item['qty'] * existing_item['price']
                    else:
                        st.session_state.invoice_items.append({
                            "item": selected_item,
                            "qty": qty,
                            "price": price,
                            "total": total
                        })
                st.success(f"✅ Added {len(selected_items)} item(s)")
                st.rerun()

            st.divider()
            # Tax & Discount for invoice
            col1, col2, col3 = st.columns(3)
            with col1:
                tax_type = st.selectbox(t('tax_type'), [t('without_tax'), t('with_tax')], key="inv_tax_type_select")
                st.session_state.inv_tax_type = tax_type
            with col2:
                tax_rate = st.number_input(t('tax_rate_percent'), 0.0, 100.0, st.session_state.inv_tax_rate, step=0.5, disabled=(tax_type == t('without_tax')))
                st.session_state.inv_tax_rate = tax_rate if tax_type == t('with_tax') else 0.0
            with col3:
                discount_input = st.number_input(t('discount_rs'), 0.0, step=10.0, key="inv_discount")

            if st.session_state.invoice_items:
                st.markdown("#### 📦 Items in Invoice")
                # display items similar to estimate
                for idx, item in enumerate(st.session_state.invoice_items):
                    item_subtotal = Decimal(str(item['total']))
                    item_tax = (item_subtotal * Decimal(str(st.session_state.inv_tax_rate)) / Decimal("100")) if tax_type == t('with_tax') else Decimal("0.00")
                    item_total_with_tax = item_subtotal + item_tax
                    col1, col2, col3, col4 = st.columns([3,1,1,1])
                    col1.write(item['item'])
                    col2.write(f"Rs {item['price']:,.0f}")
                    col3.write(f"{item['qty']}")
                    col4.write(f"Rs {item_total_with_tax:,.0f}")

                subtotal = sum(Decimal(str(item['total'])) for item in st.session_state.invoice_items)
                total_tax = (subtotal * Decimal(str(st.session_state.inv_tax_rate)) / Decimal("100")) if tax_type == t('with_tax') else Decimal("0.00")
                grand_total = subtotal + total_tax - Decimal(str(discount_input))

                st.markdown("#### 📊 Summary")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Subtotal", f"Rs {subtotal:,.2f}")
                c2.metric("Tax", f"Rs {total_tax:,.2f}")
                c3.metric("Discount", f"Rs {Decimal(str(discount_input)):,.2f}")
                c4.metric("Grand Total", f"Rs {grand_total:,.2f}")

                with st.form("create_invoice_final"):
                    invoice_date = st.date_input("📅 Invoice Date", value=datetime.now().date(), key="inv_date")
                    notes = st.text_area("📝 Notes (Optional)")
                    col1, col2 = st.columns(2)
                    with col1:
                        submit_inv = st.form_submit_button("✅ Create Invoice", type="primary", width="stretch")
                    with col2:
                        clear_inv = st.form_submit_button("🗑️ Clear All", width="stretch")

                    if submit_inv:
                        if not st.session_state.invoice_items:
                            st.error("❌ Cannot create invoice: No items")
                        else:
                            invoice_id = get_next_id("INV", "invoices")
                            invoice = {
                                "InvoiceID": invoice_id,
                                "Date": invoice_date.strftime("%Y-%m-%d"),
                                "CustomerID": customer_id,
                                "ItemsJSON": json.dumps(st.session_state.invoice_items, default=str),
                                "Subtotal": str(subtotal),
                                "TaxType": tax_type,
                                "TaxRate": str(st.session_state.inv_tax_rate),
                                "Tax": str(total_tax),
                                "Discount": str(discount_input),
                                "Total": str(grand_total),
                                "Status": "Posted",
                                "Notes": notes
                            }
                            invoices = load_table("invoices")
                            invoices = pd.concat([invoices, pd.DataFrame([invoice])], ignore_index=True)
                            save_table("invoices", invoices)
                            save_invoice_line_items(invoice_id, st.session_state.invoice_items)
                            st.success(f"✅ Invoice {invoice_id} created!")
                            st.info("💡 Switch to 'Manage Invoices' to view it")
                            st.session_state.invoice_items = []
                            st.rerun()

                    if clear_inv:
                        st.session_state.invoice_items = []
                        st.rerun()

    with tab3:
        st.markdown("### 🔄 Convert Estimate to Invoice")
        
        estimates = load_table("estimates")
        estimates = estimates[estimates["Status"] == "Draft"]
        
        if estimates.empty:
            st.info("No draft estimates to convert")
        else:
            selected_est = st.selectbox(t('select_estimate'), 
                [f"{e['EstimateID']} - {e.get('CustomerID', '')}" for _, e in estimates.iterrows()])
            est_id = selected_est.split(" - ")[0]
            estimate = estimates[estimates["EstimateID"] == est_id].iloc[0]
            
            # Show estimate details and download option
            col1, col2 = st.columns([2, 1])
            with col1:
                st.metric("Original Total", f"Rs {to_decimal_safe(estimate['Total']):,.2f}")
            with col2:
                est_id_local = str(estimate.get("EstimateID", ""))
                est_pdf_key = f"estimate_pdf_bytes_{est_id_local}"
                customer_name = ""
                try:
                    customers = load_table("customers")
                    customer_name = customers[customers["CustomerID"] == estimate["CustomerID"]].iloc[0]["CustomerName"]
                except Exception:
                    customer_name = ""

                if st.button("📥 Download Estimate PDF", width="stretch"):
                    try:
                        est_data = estimate.to_dict()
                        est_data["Customer"] = customer_name
                        items = json.loads(estimate['ItemsJSON'])
                        est_buf = generate_professional_estimate_pdf(est_data, items)
                        st.session_state[est_pdf_key] = _pdf_to_bytes(est_buf)
                    except Exception as e:
                        st.error(f"Error: {e}")

                if est_pdf_key in st.session_state:
                    if hasattr(st, "popover"):
                        with st.popover("⬇️ Estimate PDF", use_container_width=True):
                            st.download_button(
                                "Download",
                                BytesIO(st.session_state[est_pdf_key]),
                                file_name=f"{est_id_local}.pdf",
                                mime="application/pdf",
                                key=f"est_dl_{est_id_local}",
                            )
                    else:
                        with st.expander("⬇️ Estimate PDF", expanded=False):
                            st.download_button(
                                "Download",
                                BytesIO(st.session_state[est_pdf_key]),
                                file_name=f"{est_id_local}.pdf",
                                mime="application/pdf",
                                key=f"est_dl_{est_id_local}",
                            )

                render_share_options(
                    doc_type="Estimate",
                    doc_id=est_id_local,
                    customer_name=str(customer_name),
                    doc_date=str(estimate.get('Date', '')),
                    key_prefix=f"estimate_{est_id_local}",
                )

                if st.button("❌ Cancel Estimate", key=f"cancel_est_{est_id}", width="stretch"):
                    try:
                        # Safety check: only cancel Draft estimates
                        current_status = estimate.get("Status", "")
                        if current_status != "Draft":
                            st.error(f"❌ Cannot cancel: Estimate status is {current_status}")
                        else:
                            estimates.loc[estimates["EstimateID"] == est_id, "Status"] = "Cancelled"
                            save_table("estimates", estimates)
                            # Clear session state edits for this estimate if present
                            if f'edit_items_{est_id}' in st.session_state:
                                del st.session_state[f'edit_items_{est_id}']
                            st.warning("Estimate Cancelled")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error cancelling estimate: {e}")
                        logging.exception("Failed cancelling estimate %s", est_id)
            
            st.markdown("---")
            st.markdown("#### 📝 Edit Items Before Converting")
            
            # Load inventory for adding new items
            inventory = load_table("inventory")
            customers = load_table("customers")
            
            # Parse existing items
            existing_items = json.loads(estimate['ItemsJSON'])
            
            # Initialize session state for items if not exists
            if f'edit_items_{est_id}' not in st.session_state:
                st.session_state[f'edit_items_{est_id}'] = existing_items.copy()
            
            edited_items = st.session_state[f'edit_items_{est_id}']
            
            # Display and edit existing items
            st.markdown("**Current Items:**")
            
            # Table header
            col1, col2, col3, col4, col5, col6 = st.columns([0.5, 2, 1, 1.2, 1.2, 0.6])
            col1.markdown("**#**")
            col2.markdown("**Item**")
            col3.markdown("**Qty**")
            col4.markdown("**Price**")
            col5.markdown("**Total**")
            col6.markdown("")
            
            items_to_remove = []
            
            for idx, item in enumerate(edited_items):
                col1, col2, col3, col4, col5, col6 = st.columns([0.5, 2, 1, 1.2, 1.2, 0.6])
                
                with col1:
                    st.markdown(f"**{idx + 1}**")
                with col2:
                    st.write(item['item'])
                with col3:
                    new_qty = st.number_input("", min_value=1, value=int(item['qty']), key=f"qty_{est_id}_{idx}", label_visibility="collapsed")
                    item['qty'] = new_qty
                with col4:
                    price_display = to_decimal_safe(item['price'])
                    st.write(f"Rs {price_display:,.0f}")
                    new_price = to_decimal_safe(item['price'])
                    item['price'] = str(new_price)
                with col5:
                    new_total = Decimal(str(new_qty)) * new_price
                    item['total'] = str(new_total)
                    st.write(f"**Rs {new_total:,.0f}**")
                with col6:
                    if st.button("🗑️", key=f"remove_{est_id}_{idx}", help="Remove"):
                        items_to_remove.append(idx)
            
            # Remove marked items
            if items_to_remove:
                for idx in sorted(items_to_remove, reverse=True):
                    edited_items.pop(idx)
                st.session_state[f'edit_items_{est_id}'] = edited_items
                st.rerun()
            
            st.markdown("---")
            
            # Add new item section
            if not inventory.empty:
                st.markdown("**Add New Item:**")
                with st.form(f"add_item_{est_id}"):
                    col1, col2, col3 = st.columns([4, 2, 1])
                    
                    with col1:
                        new_item_name = st.selectbox(t('select_item'), inventory["ItemName"].tolist(), key=f"new_item_{est_id}")
                    with col2:
                        new_item_qty = st.number_input(t('qty'), min_value=1, value=1, key=f"new_qty_{est_id}")
                    with col3:
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.form_submit_button("➕ Add", width="stretch"):
                            item_data = inventory[inventory["ItemName"] == new_item_name].iloc[0]
                            customer_id = estimate["CustomerID"]
                            custom_price = get_customer_price(customer_id, item_data["ItemID"])
                            new_item_price = to_decimal_safe(custom_price)
                            
                            new_item = {
                                "item_id": item_data["ItemID"],
                                "item": new_item_name,
                                "qty": new_item_qty,
                                "price": str(new_item_price),
                                "total": str(Decimal(str(new_item_qty)) * new_item_price)
                            }
                            edited_items.append(new_item)
                            st.session_state[f'edit_items_{est_id}'] = edited_items
                            st.rerun()
                
                st.caption("💡 Prices are automatically loaded from Customer Pricing Matrix")
            
            st.markdown("---")
            
            # Calculate new totals
            subtotal = sum(Decimal(str(item['total'])) for item in edited_items)
            tax_type = estimate["TaxType"]
            tax_rate = float(estimate.get("TaxRate", 0))
            tax = (subtotal * Decimal(str(tax_rate)) / Decimal("100")) if tax_type == "With Tax" else Decimal("0.00")
            discount = Decimal(str(estimate.get("Discount", 0)))
            total = subtotal + tax - discount
            
            # Show updated totals
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Subtotal", f"Rs {subtotal:,.2f}")
            col2.metric("Tax", f"Rs {tax:,.2f}")
            col3.metric("Discount", f"Rs {discount:,.2f}")
            col4.metric("**New Total**", f"Rs {total:,.2f}")
            
            st.markdown("---")
            
            # Convert button
            if st.button("✅ Convert to Invoice", type="primary", width="stretch"):
                if not edited_items:
                    st.error("❌ Cannot convert: No items in the invoice")
                else:
                    invoice_id = get_next_id("INV", "invoices")
                    
                    invoice = {
                        "InvoiceID": invoice_id,
                        "Date": datetime.now().strftime("%Y-%m-%d"),
                        "CustomerID": estimate["CustomerID"],
                        "EstimateID": est_id,
                        "ItemsJSON": json.dumps(edited_items, default=str),
                        "Subtotal": str(subtotal),
                        "TaxType": tax_type,
                        "TaxRate": str(tax_rate),
                        "Tax": str(tax),
                        "Discount": str(discount),
                        "Total": str(total),
                        "Status": "Draft",
                        "Notes": estimate.get("Notes", "")
                    }
                    
                    invoices = load_table("invoices")
                    invoices = pd.concat([invoices, pd.DataFrame([invoice])], ignore_index=True)
                    save_table("invoices", invoices)
                    
                    # Dual-write: Save line items to relational table
                    save_invoice_line_items(invoice_id, edited_items)
                    
                    # Update estimate status
                    estimates.loc[estimates["EstimateID"] == est_id, "Status"] = "Converted"
                    save_table("estimates", estimates)
                    
                    # Clear session state
                    if f'edit_items_{est_id}' in st.session_state:
                        del st.session_state[f'edit_items_{est_id}']
                    
                    st.success(f"✅ Invoice {invoice_id} created with {len(edited_items)} items!")
                    st.info("💡 Switch to the 'Manage Invoices' tab to view your new invoice")
                    time.sleep(1)
                    st.rerun()
    
    with tab4:
        show_manage_invoices_section()

def show_purchases():
    """Purchases section - uses tabs for sub-pages."""
    st.title(f"📦 {t('purchases')}")
    
    tab1, tab2, tab3 = st.tabs([t('add_bill'), t('manage_bills'), t('purchase_order')])
    
    with tab1:
        st.markdown(f"### 📝 {t('add_bill')}")

        
        vendors = load_table("vendors")
        inventory = load_table("inventory")
        
        if vendors.empty:
            st.warning(f"⚠️ {t('create')} {t('vendor')} first" if t('create') != 'create' else "⚠️ Create a vendor first")
            return
        if inventory.empty:
            st.warning(f"⚠️ {t('add')} {t('item')} first" if t('add') != 'add' else "⚠️ Add items to inventory first")
            return

        # NOTE: We intentionally avoid st.form here so that changing Vendor / Items / Qty
        # refreshes totals and applies locked vendor pricing immediately.
        vendor = st.selectbox(
            t('vendor'),
            [f"{v['VendorID']} - {v['VendorName']}" for _, v in vendors.iterrows()],
            key="bill_vendor"
        )
        vendor_id = vendor.split(" - ")[0]

        tax_type = st.selectbox(t('tax_type'), [t('without_tax'), t('with_tax')], key="bill_tax_type")
        tax_rate = (
            st.number_input(t('tax_rate_percent'), 0.0, 100.0, 0.0, key="bill_tax_rate")
            if tax_type == t('with_tax')
            else 0.0
        )

        st.markdown("**Items**")
        items = []
        # Allow more than 10 items in a bill
        item_count = st.number_input(t('number_of_items'), 1, 100, 1, key="bill_item_count")

        st.markdown("---")
        for i in range(int(item_count)):
            st.markdown(f"**Item {i+1}**")
            col1, col2, col3, col4 = st.columns([3, 1.5, 1.5, 2])
            with col1:
                item_name = st.selectbox(
                    t('select_item'),
                    inventory["ItemName"].tolist(),
                    key=f"bill_item_{i}",
                    label_visibility="collapsed"
                )
            item_data = inventory[inventory["ItemName"] == item_name].iloc[0]

            with col2:
                qty = st.number_input(t('quantity'), 1, key=f"bill_qty_{i}", label_visibility="collapsed")

            with col3:
                cost_price = to_decimal_safe(item_data["CostPrice"])
                vendor_locked_price = get_vendor_price(vendor_id, item_data["ItemID"])
                default_price = vendor_locked_price if vendor_locked_price and vendor_locked_price > 0 else cost_price

                # Key includes vendor+item so changing vendor refreshes the value/lock state
                price_key = f"bill_price_{vendor_id}_{item_data['ItemID']}_{i}"
                price = st.number_input(
                    t('cost'),
                    value=float(to_decimal_safe(default_price)),
                    key=price_key,
                    disabled=bool(vendor_locked_price and vendor_locked_price > 0),
                    label_visibility="collapsed"
                )

            with col4:
                price_dec = to_decimal_safe(price)
                item_total = Decimal(str(qty)) * price_dec
                st.metric("Item Total", f"Rs {item_total:,.2f}")

            if vendor_locked_price and vendor_locked_price > 0:
                st.caption("🔒 Vendor locked price applied")

            items.append({
                "item": item_name,
                "item_id": item_data["ItemID"],
                "qty": qty,
                "price": str(price_dec),
                "total": str(item_total)
            })
            st.markdown("---")

        # Calculate totals
        subtotal = sum(to_decimal_safe(item['total']) for item in items)
        tax = (subtotal * Decimal(str(tax_rate)) / Decimal("100")) if tax_type == t('with_tax') else Decimal("0.00")
        discount = st.number_input(t('discount_rs'), 0.0, key="bill_discount")
        total = subtotal + tax - Decimal(str(discount))

        # Show summary
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Subtotal", f"Rs {subtotal:,.2f}")
        col2.metric("Tax", f"Rs {tax:,.2f}")
        col3.metric("Discount", f"Rs {Decimal(str(discount)):,.2f}")
        col4.metric("**Grand Total**", f"Rs {total:,.2f}")

        if st.button("✅ Create Bill", type="primary", width="stretch", key="bill_create_btn"):
            bill_id = get_next_id("BIL", "bills")
            vendor_name = vendors[vendors["VendorID"] == vendor_id].iloc[0]["VendorName"]
            
            bill = {
                "BillID": bill_id,
                "Date": datetime.now().strftime("%Y-%m-%d"),
                "VendorID": vendor_id,
                "ItemsJSON": json.dumps(items, default=str),
                "Subtotal": str(subtotal),
                "TaxType": tax_type,
                "TaxRate": str(tax_rate),
                "Tax": str(tax),
                "Discount": str(discount),
                "Total": str(total),
                "Status": "Draft",
                "Notes": ""
            }
            
            bills = load_table("bills")
            bills = pd.concat([bills, pd.DataFrame([bill])], ignore_index=True)
            save_table("bills", bills)
            
            # Dual-write: Save line items to relational table
            save_bill_line_items(bill_id, items)
            
            # Vendor balance will be updated when the bill is posted (consistent with invoices)
            st.success(f"✅ Bill {bill_id} created (Draft)")
            st.rerun()
    
    with tab2:
        st.markdown("### 📊 Vendor Ledger")
        
        vendors = load_table("vendors")
        bills = load_table("bills")
        
        
        if vendors.empty:
            st.info("No vendors yet")
        else:
            selected_vendor = st.selectbox(t('vendor'), 
                [f"{v['VendorID']} - {v['VendorName']}" for _, v in vendors.iterrows()], key="vend_ledger")
            vend_id = selected_vendor.split(" - ")[0]
            
            col1, col2 = st.columns(2)
            with col1:
                date_from = st.date_input(t('from_date_label'), key="vend_from")
            with col2:
                date_to = st.date_input(t('to_date_label'), datetime.now(), key="vend_to")
            
            vend_bills = bills[bills["VendorID"] == vend_id]
            vend_bills = vend_bills[(vend_bills["Date"] >= date_from.strftime("%Y-%m-%d")) & 
                                     (vend_bills["Date"] <= date_to.strftime("%Y-%m-%d"))]
            # Show all statuses including Draft, Posted, Paid
            vend_bills = vend_bills[vend_bills["Status"].isin(["Draft", "Posted", "Paid"])]
            
            if vend_bills.empty:
                st.info("No bills in this period")
            else:
                total = Decimal("0.00")
                for _, bill in vend_bills.iterrows():
                    total += to_decimal_safe(bill['Total'])
                
                st.metric("Total Payable", f"Rs {total:,.2f}")
                
                display_df = vend_bills[["BillID", "Date", "Total", "Status"]].copy()
                display_df["Total"] = display_df["Total"].apply(lambda x: f"Rs {to_decimal_safe(x):,.2f}")
                st.dataframe(display_df, hide_index=True, width="stretch")
                
                st.divider()
                st.markdown("#### Bill Actions")
                
                # Bill management with Post button
                for _, bill in vend_bills.iterrows():
                    with st.expander(f"📄 {bill['BillID']} - {bill['Date']} - Rs {to_decimal_safe(bill['Total']):,.2f} - {bill['Status']}"):
                        try:
                            # Read items from relational table (with JSON fallback)
                            items = get_bill_line_items(bill['BillID'])

                            # Display items
                            st.markdown("**Items:**")
                            for item in items:
                                st.write(f"• {item['item']} - Qty: {item['qty']} × {format_currency(item['price'])} = {format_currency(item['total'])}")
                            
                            st.markdown(f"**Total:** Rs {to_decimal_safe(bill['Total']):,.2f}")
                            st.markdown(f"**Status:** {bill['Status']}")
                            
                            # Post button for Draft bills
                            if bill["Status"] == "Draft":
                                if st.button(f"📦 Post Bill & Increase Stock", key=f"post_bill_{bill['BillID']}"):
                                    try:
                                        # Atomic transaction: stock increase + balance + status update
                                        with get_db() as conn:
                                            try:
                                                # Increase inventory with direct SQL for atomicity
                                                for item in items:
                                                    item_id = item.get('item_id') or item.get('ItemID')
                                                    qty = int(item.get("qty", 0))
                                                    if item_id:
                                                        conn.execute("UPDATE inventory SET Quantity = Quantity + ? WHERE ItemID = ?", (qty, item_id))
                                                    else:
                                                        item_name = item.get('item')
                                                        conn.execute("UPDATE inventory SET Quantity = Quantity + ? WHERE ItemName = ?", (qty, item_name))
                                                
                                                # Update vendor balance with precise Decimal arithmetic
                                                update_balance_precise(conn, "vendors", "VendorID", bill['VendorID'], to_decimal_safe(bill['Total']), "add")
                                                
                                                # Mark bill as posted
                                                conn.execute(
                                                    "UPDATE bills SET Status = 'Posted' WHERE BillID = ?",
                                                    (bill['BillID'],)
                                                )
                                                
                                                conn.commit()
                                                st.success("✅ Posted and stock increased")
                                                st.rerun()
                                            except Exception as e:
                                                conn.rollback()
                                                raise e
                                    except Exception as e:
                                        st.error(f"Error posting bill: {e}")
                                        logging.exception("Bill posting failed for %s", bill.get('BillID'))
                            
                            # We intentionally do not support marking bills "Paid".
                            # Vendor balances are managed via vendor payments.
                        except Exception as e:
                            st.error(f"Error: {e}")
    
    with tab3:
        st.markdown("### 📋 Purchase Orders")
        
        po_tab1, po_tab2 = st.tabs(["Create PO", "Manage POs"])
        
        with po_tab1:
            st.markdown("#### ➕ Create Purchase Order")
            
            vendors = load_table("vendors")
            inventory = load_table("inventory")
            
            if vendors.empty:
                st.warning("⚠️ Create a vendor first")
                return
            if inventory.empty:
                st.warning("⚠️ Add items to inventory first")
                return

            # NOTE: We intentionally avoid st.form here so that changing Vendor / Items / Qty
            # refreshes totals and applies locked vendor pricing immediately.
            vendor = st.selectbox(
                t('vendor'),
                [f"{v['VendorID']} - {v['VendorName']}" for _, v in vendors.iterrows()],
                key="po_vendor"
            )
            vendor_id = vendor.split(" - ")[0]

            tax_type = st.selectbox(t('tax_type'), [t('without_tax'), t('with_tax')], key="po_tax_type")
            tax_rate = (
                st.number_input(t('tax_rate_percent'), 0.0, 100.0, 18.0, key="po_tax_rate")
                if tax_type == t('with_tax')
                else 0.0
            )

            num_items = st.number_input(t('number_of_items_label'), 1, 20, 1, key="po_num_items")

            st.markdown("**Items**")
            items = []
            for i in range(int(num_items)):
                st.markdown(f"**{t('item')} {i+1}**")
                cols = st.columns([3, 1.5, 1.5, 2])

                with cols[0]:
                    item = st.selectbox(
                        t('select_item') if t('select_item') != 'select_item' else f"{t('item')} {i+1}",
                        inventory["ItemName"].tolist(),
                        key=f"po_item_{i}"
                    )
                    item_data = inventory[inventory["ItemName"] == item].iloc[0]

                with cols[1]:
                    qty = st.number_input(f"{t('qty')} {i+1}", 1, 10000, 1, key=f"po_qty_{i}")

                with cols[2]:
                    # Apply vendor locked price (if any) and lock the input
                    vendor_locked_price = get_vendor_price(vendor_id, item_data["ItemID"])
                    default_price = vendor_locked_price if vendor_locked_price and vendor_locked_price > 0 else to_decimal_safe(item_data["CostPrice"])
                    price_key = f"po_price_{vendor_id}_{item_data['ItemID']}_{i}"
                    price = st.number_input(
                        f"{t('cost')} {i+1}",
                        value=float(to_decimal_safe(default_price)),
                        key=price_key,
                        disabled=bool(vendor_locked_price and vendor_locked_price > 0)
                    )
                    if vendor_locked_price and vendor_locked_price > 0:
                        st.caption("🔒 Vendor locked price applied")

                with cols[3]:
                    price_dec = to_decimal_safe(price)
                    line_total = Decimal(str(qty)) * price_dec
                    st.metric(f"Total {i+1}", f"Rs {line_total:,.2f}")

                items.append({
                    "item": item,
                    "item_id": item_data["ItemID"],
                    "qty": qty,
                    "price": str(price_dec),
                    "total": str(line_total)
                })
                st.markdown("---")

            subtotal = sum(to_decimal_safe(item['total']) for item in items)
            tax = (subtotal * Decimal(str(tax_rate)) / Decimal("100")) if tax_type == t('with_tax') else Decimal("0.00")

            discount = st.number_input(t('discount_rs'), 0.0, key="po_discount")
            total = subtotal + tax - Decimal(str(discount))

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Subtotal", f"Rs {subtotal:,.2f}")
            col2.metric("Tax", f"Rs {tax:,.2f}")
            col3.metric("Discount", f"Rs {Decimal(str(discount)):,.2f}")
            col4.metric("**Grand Total**", f"Rs {total:,.2f}")

            notes = st.text_area("Notes", key="po_notes")

            if st.button("✅ Create Purchase Order", type="primary", width="stretch", key="po_create_btn"):
                if not items:
                    st.error("❌ Cannot create PO: No items")
                else:
                    po_id = get_next_id("PO", "purchase_orders", "POID")

                    po = {
                        "POID": po_id,
                        "Date": datetime.now().strftime("%Y-%m-%d"),
                        "VendorID": vendor_id,
                        "ItemsJSON": json.dumps(items, default=str),
                        "Subtotal": str(subtotal),
                        "TaxType": tax_type,
                        "TaxRate": str(tax_rate),
                        "Tax": str(tax),
                        "Discount": str(discount),
                        "Total": str(total),
                        "Status": "Pending",
                        "Notes": notes
                    }

                    pos = load_table("purchase_orders")
                    pos = pd.concat([pos, pd.DataFrame([po])], ignore_index=True)
                    save_table("purchase_orders", pos)

                    st.success(f"✅ Purchase Order {po_id} created")
                    st.rerun()
        
        with po_tab2:
            st.markdown("#### 📊 Manage Purchase Orders")
            
            pos = load_table("purchase_orders")
            vendors = load_table("vendors")
            
            if pos.empty:
                st.info("No purchase orders yet")
            else:
                # Filter options
                status_filter = st.selectbox(t('filter_by_status'), [t('all'), t('pending'), t('approved'), t('completed'), t('cancelled')])
                
                if status_filter != "All":
                    pos = pos[pos["Status"] == status_filter]
                
                # Display POs
                for _, po in pos.iterrows():
                    vendor_name = vendors[vendors["VendorID"] == po["VendorID"]].iloc[0]["VendorName"] if not vendors.empty else "Unknown"
                    
                    with st.expander(f"📋 {po['POID']} - {vendor_name} - Rs {to_decimal_safe(po['Total']):,.2f} - {po['Status']}"):
                        col1, col2, col3 = st.columns(3)
                        col1.metric("Date", po['Date'])
                        col2.metric("Vendor", vendor_name)
                        col3.metric("Total", f"Rs {to_decimal_safe(po['Total']):,.2f}")
                        
                        st.write("**Items:**")
                        items = parse_items_json(po.get('ItemsJSON', '[]'))
                        for item in items:
                            st.write(f"• {item['item']} - Qty: {item['qty']} × {format_currency(item['price'])} = {format_currency(item['total'])}")
                        
                        if po.get('Notes'):
                            st.write(f"**Notes:** {po['Notes']}")
                        
                        st.divider()
                        
                        # Action buttons
                        action_col1, action_col2, action_col3 = st.columns(3)
                        
                        with action_col1:
                            if st.button("📥 Download PDF", key=f"download_po_{po['POID']}"):
                                try:
                                    po_data = po.to_dict()
                                    po_data["Vendor"] = vendor_name
                                    pdf = generate_purchase_order_pdf(po_data, items)
                                    st.download_button(
                                        "⬇️ Download PO",
                                        pdf,
                                        file_name=f"{po['POID']}.pdf",
                                        mime="application/pdf",
                                        key=f"dl_{po['POID']}"
                                    )
                                except Exception as e:
                                    st.error(f"Error generating PDF: {e}")
                        
                        with action_col2:
                            if po["Status"] == "Pending":
                                if st.button("✅ Approve", key=f"approve_{po['POID']}"):
                                    pos.loc[pos["POID"] == po["POID"], "Status"] = "Approved"
                                    save_table("purchase_orders", pos)
                                    st.success("Purchase Order Approved")
                                    st.rerun()
                            elif po["Status"] == "Approved":
                                if st.button("📦 Mark Completed", key=f"complete_{po['POID']}"):
                                    pos.loc[pos["POID"] == po["POID"], "Status"] = "Completed"
                                    save_table("purchase_orders", pos)
                                    st.success("Purchase Order Completed")
                                    st.rerun()
                        
                        with action_col3:
                            if po["Status"] in ["Pending", "Approved"]:
                                if st.button("❌ Cancel", key=f"cancel_{po['POID']}"):
                                    pos.loc[pos["POID"] == po["POID"], "Status"] = "Cancelled"
                                    save_table("purchase_orders", pos)
                                    st.warning("Purchase Order Cancelled")
                                    st.rerun()

def show_inventory():
    """Inventory section - uses tabs for sub-pages."""
    st.title(f"📊 {t('inventory')}")
    
    tab1, tab2, tab3, tab4, tab5 = st.tabs([t('stock_list'), t('low_stock_alert'), f"{t('import')} CSV/Excel", f"{t('customer')} {t('price')} Matrix", f"{t('vendor')} {t('price')} Matrix"])
    
    inventory = load_table("inventory")
    
    with tab1:
        st.markdown(f"### 📊 {t('stock_list')}")
        
        # Add New Item section - always visible
        st.markdown("#### ➕ Add New Item")
        
        with st.form("add_item_form"):
            col1, col2 = st.columns(2)
            with col1:
                item_name = st.text_input(t('item_name'))
                cost = st.number_input(t('cost_price'), 0.0, step=10.0)
                sell = st.number_input(t('sell_price'), 0.0, step=10.0)
            with col2:
                qty = st.number_input(t('initial_quantity'), 0, step=1)
                reorder = st.number_input(t('reorder_level'), 10, step=1)
            
            if st.form_submit_button("✅ Add Item", width="stretch", type="primary"):
                if not item_name:
                    st.error("⚠️ Item name required")
                else:
                    item_id = get_next_id("ITM", "inventory", "ItemID")
                    new_item = {
                        "ItemID": item_id,
                        "ItemName": item_name,
                        "Quantity": qty,
                        "CostPrice": cost,
                        "SellPrice": sell,
                        "ReorderLevel": reorder,
                        "Active": "Yes"
                    }
                    inventory = pd.concat([inventory, pd.DataFrame([new_item])], ignore_index=True)
                    save_table("inventory", inventory)
                    st.success("✅ Item added")
                    st.rerun()
        
        st.markdown("---")
        
        # Display existing inventory
        if inventory.empty:
            st.info("No items in inventory yet. Add your first item above!")
        else:
            st.markdown("#### 📋 Current Inventory")
            # Display inventory in table format
            display_df = inventory[['ItemID', 'ItemName', 'Quantity', 'CostPrice', 'SellPrice', 'ReorderLevel']].copy()
            display_df['CostPrice'] = display_df['CostPrice'].apply(lambda x: f"Rs {to_decimal_safe(x):,.2f}")
            display_df['SellPrice'] = display_df['SellPrice'].apply(lambda x: f"Rs {to_decimal_safe(x):,.2f}")
            
            st.dataframe(
                display_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "ItemID": st.column_config.TextColumn("Item ID", width="small"),
                    "ItemName": st.column_config.TextColumn("Item Name", width="medium"),
                    "Quantity": st.column_config.NumberColumn("Stock Qty", width="small"),
                    "CostPrice": st.column_config.TextColumn("Cost Price", width="small"),
                    "SellPrice": st.column_config.TextColumn("Sell Price", width="small"),
                    "ReorderLevel": st.column_config.NumberColumn("Reorder Level", width="small")
                }
            )
            
            st.markdown("---")
            st.markdown("#### ✏️ Edit / Delete Items")
            
            # Reload inventory to ensure we have the latest data
            inventory = load_table("inventory")
            
            if inventory.empty:
                st.info("No items to edit. Add items above first.")
            else:
                # Edit/Delete section with selectbox
                selected_item = st.selectbox(
                    "Select Item to Edit/Delete",
                    inventory['ItemName'].tolist(),
                    key="edit_item_select"
                )
                
                if selected_item:
                    item = inventory[inventory['ItemName'] == selected_item].iloc[0]
                    
                    with st.expander(f"📝 Edit: {item['ItemName']}", expanded=True):
                        with st.form(f"edit_item_{item['ItemID']}"):
                            col1, col2 = st.columns(2)
                            with col1:
                                new_name = st.text_input(t('item_name'), value=item['ItemName'])
                                new_cost = st.number_input(t('cost_price'), value=float(to_decimal_safe(item['CostPrice'])))
                                new_sell = st.number_input(t('sell_price'), value=float(to_decimal_safe(item['SellPrice'])))
                            with col2:
                                new_qty = st.number_input(t('quantity'), value=int(item['Quantity']))
                                new_reorder = st.number_input(t('reorder_level'), value=int(item['ReorderLevel']))
                            
                            col1, col2 = st.columns(2)
                            with col1:
                                if st.form_submit_button("💾 Save Changes", width="stretch"):
                                    updates = {
                                        'ItemName': new_name,
                                        'Quantity': new_qty,
                                        'CostPrice': new_cost,
                                        'SellPrice': new_sell,
                                        'ReorderLevel': new_reorder
                                    }
                                    update_record('inventory', 'ItemID', item['ItemID'], updates)
                                    st.success("✅ Item updated")
                                    st.rerun()
                            
                            with col2:
                                if st.form_submit_button("🗑️ Delete Item", width="stretch"):
                                    if can_delete_item(item['ItemID']):
                                        soft_delete_record('inventory', 'ItemID', item['ItemID'])
                                        st.success("✅ Item deleted")
                                        st.rerun()
                                    else:
                                        st.error("❌ Cannot delete: Item exists in invoices or bills")
    
    with tab2:
        st.markdown("### ⚠️ Low Stock Alert")
        
        low_stock = inventory[inventory["Quantity"] <= inventory["ReorderLevel"]]
        
        if low_stock.empty:
            st.success("✅ All items are well-stocked")
        else:
            st.warning(f"⚠️ {len(low_stock)} item(s) below reorder level")
            display_df = low_stock[["ItemName", "Quantity", "ReorderLevel"]].copy()
            display_df.columns = ["Item", "Current Qty", "Reorder Level"]
            st.dataframe(display_df, hide_index=True, width="stretch")
    
    with tab3:
        st.markdown("### 📥 Import Inventory from CSV/Excel")
        
        st.info("""
        **Expected columns:** ItemName, CostPrice, SellPrice, Quantity, ReorderLevel (or LowStockThreshold)
        
        - New items will be created
        - Existing items (matched by ItemName) will be updated
        """)
        
        uploaded_file = st.file_uploader("Choose CSV or Excel file", type=['csv', 'xlsx', 'xls'])
        
        if uploaded_file:
            df_preview, error = import_inventory_from_file(uploaded_file)
            
            if error:
                st.error(error)
            else:
                st.success(f"✅ File valid: {len(df_preview)} items found")
                st.dataframe(df_preview, width="stretch")
                
                if st.button("✅ Confirm Import", type="primary"):
                    success, message = apply_inventory_import(df_preview)
                    if success:
                        st.success(message)
                        st.rerun()
                    else:
                        st.error(message)
    
    with tab4:
        st.markdown("### 💰 Customer Pricing Matrix")
        st.markdown("Set fixed prices for each customer-item combination. Prices are locked and auto-load in invoices.")
        st.markdown("---")
        
        customers = load_table("customers")
        
        if inventory.empty:
            st.warning("⚠️ Add items to inventory first")
            return
        
        if customers.empty:
            st.warning("⚠️ Add customers first")
            return
        
        # Search/Filter
        col1, col2 = st.columns([3, 1])
        with col1:
            search_term = st.text_input(t('search_items'), placeholder=t('filter_by_name'))
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔄 Refresh Matrix", width="stretch"):
                st.rerun()
        
        # Get pricing matrix
        matrix_df = get_pricing_matrix()
        
        if matrix_df.empty:
            st.info("No pricing data available")
            return
        
        # Apply search filter
        if search_term:
            matrix_df = matrix_df[matrix_df["ItemName"].str.contains(search_term, case=False, na=False)]
        
        st.markdown("---")
        
        # Info box
        st.info("""
        **📋 Instructions:**
        - Edit prices directly in the table below
        - If no custom price is set, the default price is used
        - Negative prices are not allowed
        - Click 'Save All Pricing' to apply changes
        """)
        
        # Configure columns for data editor
        column_config = {
            "ItemID": st.column_config.TextColumn("Item ID", disabled=True, width="small"),
            "ItemName": st.column_config.TextColumn("Item Name", disabled=True, width="medium"),
            "Quantity": st.column_config.NumberColumn("Stock", disabled=True, width="small"),
            "CostPrice": st.column_config.NumberColumn("Cost Price", disabled=True, format="Rs %.2f", width="small"),
            "DefaultPrice": st.column_config.NumberColumn("Default Price", disabled=True, format="Rs %.2f", width="small")
        }
        
        # Add customer columns
        for _, customer in customers.iterrows():
            column_config[customer["CustomerName"]] = st.column_config.NumberColumn(
                customer["CustomerName"],
                help=f"Custom price for {customer['CustomerName']}",
                min_value=0.0,
                format="Rs %.2f",
                width="medium"
            )
        
        # Editable data editor
        edited_df = st.data_editor(
            matrix_df,
            column_config=column_config,
            width="stretch",
            hide_index=True,
            num_rows="fixed",
            key="pricing_matrix_editor"
        )
        
        st.markdown("---")
        
        # Save button
        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            st.metric("Total Items", len(edited_df))
        with col2:
            st.metric("Total Customers", len(customers))
        with col3:
            if st.button("💾 Save All Pricing", type="primary", width="stretch"):
                # Validate no negative prices
                has_negative = False
                for col in edited_df.columns:
                    if col not in ["ItemID", "ItemName", "Quantity", "CostPrice", "DefaultPrice"]:
                        if (edited_df[col] < 0).any():
                            has_negative = True
                            break
                
                if has_negative:
                    st.error("❌ Negative prices are not allowed!")
                else:
                    with st.spinner("Saving pricing matrix..."):
                        saved_count = save_pricing_matrix(edited_df, customers)
                        st.success(f"✅ Successfully saved {saved_count} price records!")
                        st.balloons()

    # ---------------- Vendor Pricing Matrix ----------------
    with tab5:
        st.markdown("### 💰 Vendor Pricing Matrix")
        st.markdown("Set locked purchase prices for each vendor-item combination. Locked prices will auto-load (and lock) in Bills and Purchase Orders when a vendor is selected.")
        st.markdown("---")

        vendors = load_table("vendors")

        if inventory.empty:
            st.warning("⚠️ Add items to inventory first")
            return

        if vendors.empty:
            st.warning("⚠️ Add vendors first")
            return

        # Search/Filter
        col1, col2 = st.columns([3, 1])
        with col1:
            search_term = st.text_input(t('search_items'), placeholder=t('filter_by_name'), key="vend_pr_search")
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔄 Refresh Matrix", width="stretch", key="vend_pr_refresh"):
                st.rerun()

        # Build vendor pricing matrix base from inventory
        base_df = inventory[["ItemID", "ItemName", "Quantity", "CostPrice"]].copy()
        base_df.rename(columns={"CostPrice": "CostPrice",}, inplace=True)
        base_df["DefaultPrice"] = base_df["CostPrice"].apply(lambda x: float(to_decimal_safe(x)))

        # Add vendor columns (vectorized for performance)
        vendor_prices = load_table("vendor_item_prices")
        if vendor_prices.empty:
            # No locked prices yet — initialize vendor columns to 0.0
            for _, vendor in vendors.iterrows():
                base_df[vendor["VendorName"]] = 0.0
        else:
            vp = vendor_prices[["VendorID", "ItemID", "LockedPrice"]].copy()
            # Ensure numeric
            vp["LockedPrice"] = vp["LockedPrice"].apply(lambda x: float(to_decimal_safe(x, Decimal("0.00"))))
            # Pivot so each vendor is a column
            try:
                pivot = vp.pivot(index="ItemID", columns="VendorID", values="LockedPrice")
            except Exception:
                pivot = pd.DataFrame()

            if not pivot.empty:
                # Rename VendorID columns to VendorName where possible
                rename_map = {}
                for vid in pivot.columns.tolist():
                    match = vendors[vendors["VendorID"] == vid]
                    rename_map[vid] = match.iloc[0]["VendorName"] if not match.empty else vid

                pivot = pivot.rename(columns=rename_map)
                # Merge pivot with base_df on ItemID
                base_df = base_df.merge(pivot, how="left", left_on="ItemID", right_index=True)
                # Fill missing vendor prices with 0.0
                for col in pivot.columns:
                    if col not in base_df.columns:
                        base_df[col] = 0.0
                base_df = base_df.fillna(0.0)
            else:
                # Fallback: initialize vendor columns
                for _, vendor in vendors.iterrows():
                    base_df[vendor["VendorName"]] = 0.0

        # Auto-fill vendor columns with DefaultPrice where value is 0.0 (so matrix isn't empty)
        vendor_cols = [c for c in base_df.columns if c not in ["ItemID", "ItemName", "Quantity", "CostPrice", "DefaultPrice"]]
        for col in vendor_cols:
            # Ensure column exists and numeric
            base_df[col] = base_df[col].fillna(0.0).astype(float)
            mask = (base_df[col] == 0.0)
            if mask.any():
                base_df.loc[mask, col] = base_df.loc[mask, "DefaultPrice"].astype(float)

        # Apply search filter
        if search_term:
            base_df = base_df[base_df["ItemName"].str.contains(search_term, case=False, na=False)]

        st.markdown("---")

        st.info("**📋 Instructions:**\n- Edit locked vendor prices directly in the table below.\n- If price is 0.00 it means no locked price set.\n- Click 'Save All Vendor Pricing' to apply changes")

        # Column config
        column_config = {
            "ItemID": st.column_config.TextColumn("Item ID", disabled=True, width="small"),
            "ItemName": st.column_config.TextColumn("Item Name", disabled=True, width="medium"),
            "Quantity": st.column_config.NumberColumn("Stock", disabled=True, width="small"),
            "CostPrice": st.column_config.NumberColumn("Cost Price", disabled=True, format="Rs %.2f", width="small"),
            "DefaultPrice": st.column_config.NumberColumn("Default Price", disabled=True, format="Rs %.2f", width="small")
        }

        for _, vendor in vendors.iterrows():
            column_config[vendor["VendorName"]] = st.column_config.NumberColumn(
                vendor["VendorName"],
                help=f"Locked purchase price for {vendor['VendorName']}",
                min_value=0.0,
                format="Rs %.2f",
                width="medium"
            )

        edited_df = st.data_editor(
            base_df,
            column_config=column_config,
            width="stretch",
            hide_index=True,
            num_rows="fixed",
            key="vendor_pricing_matrix_editor"
        )

        st.markdown("---")
        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            st.metric("Total Items", len(edited_df))
        with col2:
            st.metric("Total Vendors", len(vendors))
        with col3:
            if st.button("💾 Save All Vendor Pricing", type="primary", width="stretch"):
                # Validate no negative prices
                has_negative = False
                for col in edited_df.columns:
                    if col not in ["ItemID", "ItemName", "Quantity", "CostPrice", "DefaultPrice"]:
                        if (edited_df[col] < 0).any():
                            has_negative = True
                            break

                if has_negative:
                    st.error("❌ Negative prices are not allowed!")
                else:
                    with st.spinner("Saving vendor pricing matrix..."):
                        save_count = 0
                        for idx, row in edited_df.iterrows():
                            item_id = row["ItemID"]
                            for _, vendor in vendors.iterrows():
                                vendor_name = vendor["VendorName"]
                                vendor_id = vendor["VendorID"]
                                if vendor_name in row:
                                    new_price = Decimal(str(row[vendor_name]))
                                    existing_price = get_vendor_price(vendor_id, item_id)
                                    # If price changed, apply update or delete
                                    if new_price != existing_price:
                                        if new_price > 0:
                                            save_vendor_item_price(vendor_id, item_id, new_price)
                                            save_count += 1
                                        else:
                                            # new_price == 0 -> remove existing locked price if any
                                            if existing_price > 0:
                                                delete_vendor_item_price(vendor_id, item_id)
                                                save_count += 1

                        st.success(f"✅ Successfully applied {save_count} vendor price changes!")
                        st.balloons()

def show_expenses():
    """Expenses section - uses tabs for sub-pages."""
    st.title(f"💸 {t('expenses')}")
    
    tab1, tab2 = st.tabs([t('add_expense'), t('expense_list')])
    
    with tab1:
        st.markdown(f"### ➕ {t('add_expense')}")
        
        with st.form("add_expense"):
            category = st.selectbox("Category", ["Salary", "Utilities", "Rent", "Marketing", "Fuel", "Food", "Other"])
            description = st.text_input("Description")
            amount = st.number_input("Amount (Rs)", 0.0)
            
            if st.form_submit_button("✅ Record Expense"):
                expense_id = get_next_id("EXP", "expenses")
                
                expense = {
                    "ExpenseID": expense_id,
                    "Date": datetime.now().strftime("%Y-%m-%d"),
                    "Category": category,
                    "Description": description,
                    "Amount": amount,
                    "Status": "Recorded"
                }
                
                expenses = load_table("expenses")
                expenses = pd.concat([expenses, pd.DataFrame([expense])], ignore_index=True)
                save_table("expenses", expenses)
                
                st.success("✅ Expense recorded")
                st.rerun()
    
    with tab2:
        st.markdown("### 📋 Expense List")
        
        expenses = load_table("expenses")
        
        if expenses.empty:
            st.info("No expenses recorded yet")
        else:
            col1, col2 = st.columns(2)
            with col1:
                date_from = st.date_input("From", key="exp_list_from")
            with col2:
                date_to = st.date_input("To", datetime.now(), key="exp_list_to")
            
            filtered = expenses[(expenses["Date"] >= date_from.strftime("%Y-%m-%d")) & 
                                (expenses["Date"] <= date_to.strftime("%Y-%m-%d"))]
            
            if filtered.empty:
                st.info("No expenses in this period")
            else:
                total = sum(to_decimal_safe(e['Amount']) for _, e in filtered.iterrows())
                st.metric("Total Expenses", f"Rs {total:,.2f}")
                
                display_df = filtered[["Date", "Category", "Description", "Amount"]].copy()
                display_df["Amount"] = display_df["Amount"].apply(lambda x: f"Rs {to_decimal_safe(x):,.2f}")
                st.dataframe(display_df, hide_index=True, width="stretch")

def show_payments():
    """Payments section - Receive from Customers and Pay to Vendors."""
    st.title(f"💵 {t('payments')}")
    
    tab1, tab2, tab3, tab4 = st.tabs([f"💰 {t('receive_payment')}", f"💸 {t('make_payment')}", f"💼 {t('loans')}", f"📊 {t('payment')} {t('history')}" if t('history') != 'history' else "📊 Payments History"])

    with tab1:
        st.markdown(f"### {t('receive_payment')}")
        st.markdown("<br>", unsafe_allow_html=True)
        
        customers = load_table("customers")
        
        if customers.empty:
            st.warning("⚠️ No customers available")
        else:
            # Filter customers with balance > 0
            customers_with_balance = customers[customers['OpenBalance'].apply(lambda x: to_decimal_safe(x) > 0)]
            
            if customers_with_balance.empty:
                st.info(t('no_outstanding_customer'))
            else:
                with st.form("receive_payment"):
                    selected_customer = st.selectbox(
                        "Select Customer",
                        [f"{c['CustomerID']} - {c['CustomerName']} (Balance: Rs {to_decimal_safe(c['OpenBalance']):,.2f})" 
                         for _, c in customers_with_balance.iterrows()]
                    )
                    cust_id = selected_customer.split(" - ")[0]
                    cust_data = customers[customers["CustomerID"] == cust_id].iloc[0]
                    current_balance = to_decimal_safe(cust_data['OpenBalance'])
                    
                    st.metric("Current Balance", f"Rs {current_balance:,.2f}")
                    
                    amount = st.number_input("Payment Amount (Rs)", 0.0, float(current_balance), 0.0)
                    payment_method = st.selectbox("Payment Method", ["Cash", "Bank Transfer", "Cheque", "Card"])
                    notes = st.text_area("Notes (Optional)")
                    
                    if st.form_submit_button("✅ Receive Payment", width="stretch", type="primary"):
                        if amount <= 0:
                            st.error("Amount must be greater than 0")
                        elif amount > current_balance:
                            st.error(f"Amount exceeds balance (Rs {current_balance:,.2f})")
                        else:
                            success, message = record_customer_payment(cust_id, Decimal(str(amount)), payment_method, notes)
                            if success:
                                st.success(f"✅ {message}")
                                st.success(f"New balance: Rs {current_balance - Decimal(str(amount)):,.2f}")
                                st.rerun()
                            else:
                                st.error(message)
    
    with tab2:
        st.markdown("### Pay Vendor")
        st.markdown("<br>", unsafe_allow_html=True)
        
        vendors = load_table("vendors")
        
        if vendors.empty:
            st.warning("⚠️ No vendors available")
        else:
            # Filter vendors with balance > 0
            vendors_with_balance = vendors[vendors['OpenBalance'].apply(lambda x: to_decimal_safe(x) > 0)]
            
            if vendors_with_balance.empty:
                st.info(t('no_outstanding_vendor'))
            else:
                vendor_options = [f"{v['VendorID']} - {v['VendorName']} (Balance: Rs {to_decimal_safe(v['OpenBalance']):,.2f})" 
                                 for _, v in vendors_with_balance.iterrows()]
                selected_vendor = st.selectbox(
                    "Select Vendor",
                    vendor_options,
                    key="pay_vendor_select"
                )
                vend_id = selected_vendor.split(" - ")[0]
                vend_data = vendors[vendors["VendorID"] == vend_id].iloc[0]
                current_balance = to_decimal_safe(vend_data['OpenBalance'])
                st.metric("Current Balance", f"Rs {current_balance:,.2f}")
                with st.form("pay_vendor"):
                    amount = st.number_input("Payment Amount (Rs)", 0.0, float(current_balance), 0.0)
                    payment_method = st.selectbox("Payment Method", ["Cash", "Bank Transfer", "Cheque", "Card"])
                    notes = st.text_area("Notes (Optional)")
                    if st.form_submit_button("✅ Make Payment", width="stretch", type="primary"):
                        if amount <= 0:
                            st.error("Amount must be greater than 0")
                        elif amount > current_balance:
                            st.error(f"Amount exceeds balance (Rs {current_balance:,.2f})")
                        else:
                            success, message = record_vendor_payment(vend_id, Decimal(str(amount)), payment_method, notes)
                            if success:
                                st.success(f"✅ {message}")
                                st.success(f"New balance: Rs {current_balance - Decimal(str(amount)):,.2f}")
                                st.rerun()
                            else:
                                st.error(message)
    
    with tab3:
        st.markdown("### 💼 Loan Transactions")
        st.markdown("<br>", unsafe_allow_html=True)

        parties = load_table("loan_parties")
        if parties.empty:
            st.info("No loan parties found. Add parties from the Loans page.")
        else:
            with st.form("loan_txn_form"):
                selected = st.selectbox("Select Party", [f"{r['PartyID']} - {r['PartyName']} (Bal: Rs {to_decimal_safe(r.get('Balance',0)):,.2f})" for _, r in parties.iterrows()])
                party_id = selected.split(" - ")[0]
                direction = st.selectbox("Direction", ["Received", "Given"], help="Received = we received loan (increase party balance); Given = we gave loan (decrease party balance)")
                amount = st.number_input("Amount (Rs)", min_value=0.0)
                notes = st.text_area("Notes (optional)")
                if st.form_submit_button("✅ Record Loan Transaction", type="primary", width="stretch"):
                    if amount <= 0:
                        st.error("Amount must be greater than 0")
                    else:
                        success, msg = record_loan_transaction(party_id, Decimal(str(amount)), direction, notes)
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

    with tab4:
        st.markdown("### Payments History")
        st.markdown("View all payments received from customers and paid to vendors")
        st.markdown("<br>", unsafe_allow_html=True)

        # Load data
        customers = load_table("customers")
        vendors = load_table("vendors")
        customer_payments = load_table("customer_payments")
        vendor_payments = load_table("vendor_payments")
        
        # Build list of all parties (customers and vendors)
        all_parties = ["All"]
        if not customers.empty:
            all_parties.extend(customers["CustomerName"].tolist())
        if not vendors.empty:
            all_parties.extend(vendors["VendorName"].tolist())

        # Filter options
        col1, col2, col3 = st.columns([2, 2, 2])
        with col1:
            payment_type = st.selectbox("Filter by Type", ["All Payments", "Payments Received", "Payments Sent"]) 
        with col2:
            sort_by = st.selectbox("Sort by", ["Date (Newest First)", "Date (Oldest First)", "Amount (High to Low)", "Amount (Low to High)"])
        with col3:
            selected_party = st.selectbox("Filter by Customer/Vendor", all_parties)

        st.divider()

        # Combine both payment types
        all_payments = []

        if payment_type in ["All Payments", "Payments Received"]:
            for _, pay in customer_payments.iterrows():
                cust = customers[customers["CustomerID"] == pay["CustomerID"]]
                cust_name = cust.iloc[0]["CustomerName"] if not cust.empty else "Unknown"
                all_payments.append({
                    "Type": "Received",
                    "PaymentID": pay["PaymentID"],
                    "Date": pay["Date"],
                    "Party": cust_name,
                    "Amount": to_decimal_safe(pay["Amount"]),
                    "Method": pay["PaymentMethod"],
                    "Notes": pay.get("Notes", "-"),
                    "Icon": "💰"
                })

        if payment_type in ["All Payments", "Payments Sent"]:
            for _, pay in vendor_payments.iterrows():
                vend = vendors[vendors["VendorID"] == pay["VendorID"]]
                vend_name = vend.iloc[0]["VendorName"] if not vend.empty else "Unknown"
                all_payments.append({
                    "Type": "Sent",
                    "PaymentID": pay["PaymentID"],
                    "Date": pay["Date"],
                    "Party": vend_name,
                    "Amount": to_decimal_safe(pay["Amount"]),
                    "Method": pay["PaymentMethod"],
                    "Notes": pay.get("Notes", "-"),
                    "Icon": "💸"
                })

        if not all_payments:
            st.info("No payment records found")
        else:
            # Filter by selected party
            if selected_party != "All":
                all_payments = [p for p in all_payments if p["Party"] == selected_party]

            # Sort payments
            if sort_by == "Date (Newest First)":
                all_payments.sort(key=lambda x: x["Date"], reverse=True)
            elif sort_by == "Date (Oldest First)":
                all_payments.sort(key=lambda x: x["Date"])
            elif sort_by == "Amount (High to Low)":
                all_payments.sort(key=lambda x: x["Amount"], reverse=True)
            elif sort_by == "Amount (Low to High)":
                all_payments.sort(key=lambda x: x["Amount"])

            # Display summary
            total_received = sum(p["Amount"] for p in all_payments if p["Type"] == "Received")
            total_sent = sum(p["Amount"] for p in all_payments if p["Type"] == "Sent")

            col1, col2, col3 = st.columns(3)
            col1.metric("Total Payments", len(all_payments))
            col2.metric("Total Received", f"Rs {total_received:,.2f}")
            col3.metric("Total Sent", f"Rs {total_sent:,.2f}")

            st.divider()

            # Display payments
            if not all_payments:
                st.info(f"No payments found for '{selected_party}'")
            else:
                for payment in all_payments:
                    type_badge = "🟢 Received" if payment["Type"] == "Received" else "🔴 Sent"
                    
                    with st.expander(f"{payment['Icon']} {payment['PaymentID']} - {payment['Party']} - Rs {payment['Amount']:,.2f} ({type_badge})"):
                        col1, col2 = st.columns(2)
                        with col1:
                            st.write(f"**Type:** {type_badge}")
                            st.write(f"**Party:** {payment['Party']}")
                            st.write(f"**Amount:** Rs {payment['Amount']:,.2f}")
                        with col2:
                            st.write(f"**Date:** {payment['Date']}")
                            st.write(f"**Method:** {payment['Method']}")
                            st.write(f"**Payment ID:** {payment['PaymentID']}")
                        
                        if payment['Notes'] and payment['Notes'] != "-":
                            st.write(f"**Notes:** {payment['Notes']}")

def show_reports():
    """Reports section - Enterprise-level financial reporting."""
    st.title(f"📊 {t('reports')}")
    st.markdown(f"### {t('comprehensive_financial_analysis')}" if t('comprehensive_financial_analysis') != 'comprehensive_financial_analysis' else "### Comprehensive Financial Analysis")
    st.divider()
    
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        f"📈 {t('sales_profit')}", 
        f"💰 {t('profit_loss')}", 
        f"👥 {t('customer_ledger')}", 
        f"🏢 {t('vendor_ledger')}", 
        f"📦 {t('item_wise_sales')}",
        f"💵 {t('cash_flow')}"
    ])
    
    with tab1:
        st.markdown(f"#### {t('sales_profit')} {t('summary')}")
        st.markdown("<br>", unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            date_from = st.date_input(t('from_date'), key="rpt_sales_profit_from")
        with col2:
            date_to = st.date_input(t('to_date'), datetime.now(), key="rpt_sales_profit_to")
        
        st.divider()
        
        # Load data
        invoices = load_table("invoices")
        bills = load_table("bills")
        expenses = load_table("expenses")
        
        date_from_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")
        
        invoices_range = invoices[(invoices['Date'] >= date_from_str) & (invoices['Date'] <= date_to_str)]
        bills_range = bills[(bills['Date'] >= date_from_str) & (bills['Date'] <= date_to_str)]
        expenses_range = expenses[(expenses['Date'] >= date_from_str) & (expenses['Date'] <= date_to_str)]
        
        # Calculate totals - only count invoices that have been posted (Posted or Paid)
        posted_invoices_range = invoices_range[invoices_range['Status'].isin(['Posted', 'Paid'])]
        total_sales = sum(to_decimal_safe(inv['Total']) for _, inv in posted_invoices_range.iterrows())
        total_cost = calculate_cogs_from_invoices(posted_invoices_range)  # Calculate actual COGS from items sold
        total_expenses = sum(to_decimal_safe(exp['Amount']) for _, exp in expenses_range.iterrows())
        gross_profit = total_sales - total_cost
        net_profit = gross_profit - total_expenses
        
        # Summary cards with professional styling
        st.markdown("### Key Metrics")
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #10b981 0%, #059669 100%); 
                        padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px rgba(16,185,129,0.25);'>
                <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='Total Sales'>Total Sales</p>
                <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>Rs {total_sales:,.2f}</h2>
            </div>
            """, unsafe_allow_html=True)
        
        with col2:
            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); 
                        padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px rgba(59,130,246,0.25);'>
                <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='Cost of Goods'>Cost of Goods</p>
                <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>Rs {total_cost:,.2f}</h2>
            </div>
            """, unsafe_allow_html=True)
        
        with col3:
            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); 
                        padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px rgba(245,158,11,0.25);'>
                <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='Operating Expenses'>Operating Expenses</p>
                <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>Rs {total_expenses:,.2f}</h2>
            </div>
            """, unsafe_allow_html=True)
        
        with col4:
            profit_gradient = 'linear-gradient(135deg, #10b981 0%, #059669 100%)' if net_profit >= 0 else 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)'
            profit_shadow = 'rgba(16,185,129,0.25)' if net_profit >= 0 else 'rgba(239,68,68,0.25)'
            st.markdown(f"""
            <div style='background: {profit_gradient}; 
                        padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px {profit_shadow};'>
                <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' title='Net Profit'>Net Profit</p>
                <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>Rs {net_profit:,.2f}</h2>
            </div>
            """, unsafe_allow_html=True)
        
        st.markdown("<br><br>", unsafe_allow_html=True)
        
        # Structured breakdown section
        st.markdown("### Financial Breakdown")
        
        summary_data = {
            "Category": ["Revenue (Sales)", "Less: Cost of Goods Sold", "Gross Profit", "Less: Operating Expenses", "Net Profit"],
            "Amount": [
                f"Rs {total_sales:,.2f}",
                f"Rs {total_cost:,.2f}",
                f"Rs {gross_profit:,.2f}",
                f"Rs {total_expenses:,.2f}",
                f"Rs {net_profit:,.2f}"
            ],
            "Type": ["Revenue", "Expense", "Result", "Expense", "Result"]
        }
        
        summary_df = pd.DataFrame(summary_data)
        
        # Styled dataframe
        st.dataframe(
            summary_df,
            hide_index=True,
            width="stretch",
            column_config={
                "Category": st.column_config.TextColumn("Category", width="medium"),
                "Amount": st.column_config.TextColumn("Amount", width="medium"),
                "Type": st.column_config.TextColumn("Type", width="small")
            }
        )
        
        # Download button
        st.markdown("<br>", unsafe_allow_html=True)
        
        # Prepare data for PDF export
        report_data = {
            "Financial Summary": [
                ("Revenue (Sales)", f"Rs {total_sales:,.2f}", 0, False),
                ("Cost of Goods Sold", f"Rs {total_cost:,.2f}", 0, False),
                ("Gross Profit", f"Rs {gross_profit:,.2f}", 0, False),
                ("Operating Expenses", f"Rs {total_expenses:,.2f}", 0, False),
                ("Net Profit", f"Rs {net_profit:,.2f}", 0, True),
            ]
        }
        
        date_range_text = f"Period: {date_from_str} to {date_to_str}"
        pdf_bytes = export_report_to_pdf("SALES & PROFIT SUMMARY", report_data, date_range_text, get_active_company_name())
        
        st.download_button(
            "📥 Download Report (PDF)",
            pdf_bytes,
            file_name=f"sales_profit_summary_{date_from_str}_to_{date_to_str}.pdf",
            mime="application/pdf",
            width="content"
        )
    
    with tab2:
        st.markdown("#### Profit & Loss Statement")
        st.markdown("##### Financial Performance Report")
        st.markdown("<br>", unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            date_from = st.date_input("From Date", key="rpt_pl_from")
        with col2:
            date_to = st.date_input("To Date", datetime.now(), key="rpt_pl_to")
        
        st.divider()
        
        invoices = load_table("invoices")
        bills = load_table("bills")
        expenses = load_table("expenses")
        
        date_from_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")
        
        invoices_range = invoices[(invoices['Date'] >= date_from_str) & (invoices['Date'] <= date_to_str)]
        bills_range = bills[(bills['Date'] >= date_from_str) & (bills['Date'] <= date_to_str)]
        expenses_range = expenses[(expenses['Date'] >= date_from_str) & (expenses['Date'] <= date_to_str)]
        
        # Only count invoices that have been posted (Posted or Paid) for accurate revenue
        posted_invoices_range = invoices_range[invoices_range['Status'].isin(['Posted', 'Paid'])]
        revenue = sum(to_decimal_safe(inv['Total']) for _, inv in posted_invoices_range.iterrows())
        cogs = calculate_cogs_from_invoices(posted_invoices_range)  # Calculate actual COGS from items sold
        expenses_total = sum(to_decimal_safe(exp['Amount']) for _, exp in expenses_range.iterrows())
        gross_profit = revenue - cogs
        net_profit = gross_profit - expenses_total
        
        # Professional P&L Statement with centered layout
        col_spacer1, col_center, col_spacer2 = st.columns([1, 3, 1])
        
        with col_center:
            # Header
            st.markdown(f"""
            <div style='text-align: center; margin-bottom: 32px;'>
                <h3 style='color: #1F2937; margin: 0; font-size: 24px;'>PROFIT & LOSS STATEMENT</h3>
                <p style='color: #6B7280; margin-top: 8px; font-size: 14px;'>Period: {date_from.strftime('%d %b %Y')} to {date_to.strftime('%d %b %Y')}</p>
            </div>
            """, unsafe_allow_html=True)
            
            # Revenue Section
            st.markdown("##### REVENUE")
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write("Sales Revenue")
            with col2:
                st.markdown(f"<p style='text-align: right; color: #10b981; font-weight: 600;'>Rs {revenue:,.2f}</p>", unsafe_allow_html=True)
            
            st.divider()
            
            # COGS Section
            st.markdown("##### COST OF GOODS SOLD")
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write("Cost of Purchases")
            with col2:
                st.markdown(f"<p style='text-align: right; color: #ef4444; font-weight: 600;'>Rs {cogs:,.2f}</p>", unsafe_allow_html=True)
            
            st.divider()
            
            # Gross Profit
            gross_profit_color = '#10b981' if gross_profit >= 0 else '#ef4444'
            st.markdown(f"""
            <div style='background: #F9FAFB; padding: 16px; border-radius: 8px; margin: 20px 0;'>
                <div style='display: flex; justify-content: space-between;'>
                    <span style='color: #1F2937; font-weight: 700; font-size: 17px;'>GROSS PROFIT</span>
                    <span style='color: {gross_profit_color}; font-weight: 700; font-size: 18px;'>Rs {gross_profit:,.2f}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)
            
            # Operating Expenses Section
            st.markdown("##### OPERATING EXPENSES")
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write("Total Operating Expenses")
            with col2:
                st.markdown(f"<p style='text-align: right; color: #ef4444; font-weight: 600;'>Rs {expenses_total:,.2f}</p>", unsafe_allow_html=True)
            
            st.divider()
            
            # Net Profit
            profit_gradient = 'linear-gradient(135deg, #10b981 0%, #059669 100%)' if net_profit >= 0 else 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)'
            profit_status = 'Profitable Period ✓' if net_profit >= 0 else 'Loss Period'
            
            st.markdown(f"""
            <div style='background: {profit_gradient}; padding: 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); margin-top: 20px;'>
                <div style='display: flex; justify-content: space-between; align-items: center;'>
                    <span style='color: white; font-weight: 800; font-size: 20px;'>NET PROFIT</span>
                    <span style='color: white; font-weight: 800; font-size: 24px;'>Rs {net_profit:,.2f}</span>
                </div>
                <div style='text-align: right; margin-top: 8px;'>
                    <span style='color: rgba(255,255,255,0.9); font-size: 13px;'>{profit_status}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)
        
        st.markdown("<br><br>", unsafe_allow_html=True)
        
        # Download button
        report_data = {
            "REVENUE": [
                ("Sales Revenue", f"Rs {revenue:,.2f}", 0, False),
            ],
            "COST OF GOODS SOLD": [
                ("Cost of Purchases", f"Rs {cogs:,.2f}", 0, False),
            ],
            "GROSS PROFIT": [
                ("Gross Profit", f"Rs {gross_profit:,.2f}", 0, True),
            ],
            "OPERATING EXPENSES": [
                ("Total Operating Expenses", f"Rs {expenses_total:,.2f}", 0, False),
            ],
            "NET PROFIT": [
                ("Net Profit", f"Rs {net_profit:,.2f}", 0, True),
            ]
        }
        
        date_range_text = f"Period: {date_from.strftime('%d %b %Y')} to {date_to.strftime('%d %b %Y')}"
        pdf_bytes = export_report_to_pdf("PROFIT & LOSS STATEMENT", report_data, date_range_text, get_active_company_name())
        
        st.download_button(
            "📥 Download P&L Statement (PDF)",
            pdf_bytes,
            file_name=f"profit_loss_{date_from_str}_to_{date_to_str}.pdf",
            mime="application/pdf",
            width="content"
        )
    
    with tab3:
        st.markdown("#### Customer Ledger Report")
        st.markdown("<br>", unsafe_allow_html=True)

        customers = load_table("customers")
        invoices = load_table("invoices")
        customer_payments = load_table("customer_payments")

        if customers.empty:
            st.info("ℹ️ No customers available. Add customers to generate ledger reports.")
        else:
            selected_customer = st.selectbox(
                "Select Customer",
                [f"{c['CustomerID']} - {c['CustomerName']}" for _, c in customers.iterrows()],
                key="rpt_cust_ledger_select"
            )
            cust_id = selected_customer.split(" - ")[0]
            cust_data = customers[customers["CustomerID"] == cust_id].iloc[0]

            col1, col2 = st.columns(2)
            with col1:
                date_from = st.date_input("From Date", datetime.now() - timedelta(days=30), key="rpt_cust_ledger_from")
            with col2:
                date_to = st.date_input("To Date", datetime.now(), key="rpt_cust_ledger_to")

            date_from_str = date_from.strftime("%Y-%m-%d")
            date_to_str = date_to.strftime("%Y-%m-%d")

            st.divider()

            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%); padding: 24px; border-radius: 12px; margin-bottom: 24px; color: white;'>
                <h3 style='margin: 0 0 16px 0; font-size: 22px;'>{cust_data['CustomerName']}</h3>
                <div style='display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;'>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Email</p>
                        <p style='margin: 4px 0 0 0; font-size: 15px; font-weight: 600;'>{cust_data.get('Email', 'N/A')}</p>
                    </div>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Phone</p>
                        <p style='margin: 4px 0 0 0; font-size: 15px; font-weight: 600;'>{cust_data.get('Phone', 'N/A')}</p>
                    </div>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Period</p>
                        <p style='margin: 4px 0 0 0; font-size: 15px; font-weight: 600;'>{date_from.strftime('%d %b %Y')} - {date_to.strftime('%d %b %Y')}</p>
                    </div>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Current Balance</p>
                        <p style='margin: 4px 0 0 0; font-size: 18px; font-weight: 700;'>Rs {to_decimal_safe(cust_data.get('OpenBalance', 0)):,.2f}</p>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            cust_invoices_all = invoices[
                (invoices["CustomerID"] == cust_id) &
                (invoices["Status"].isin(["Posted", "Paid"]))
            ]
            cust_invoices = cust_invoices_all[
                (cust_invoices_all["Date"] >= date_from_str) &
                (cust_invoices_all["Date"] <= date_to_str)
            ]

            cust_payments_all = customer_payments[customer_payments["CustomerID"] == cust_id]
            cust_payments = cust_payments_all[
                (cust_payments_all["Date"] >= date_from_str) &
                (cust_payments_all["Date"] <= date_to_str)
            ]

            if cust_invoices.empty and cust_payments.empty:
                st.info("ℹ️ No transactions in this period")
                return

            total_invoices = sum(to_decimal_safe(inv['Total']) for _, inv in cust_invoices.iterrows())
            total_payments = sum(to_decimal_safe(pay['Amount']) for _, pay in cust_payments.iterrows())
            net_change = total_invoices - total_payments

            current_balance = to_decimal_safe(cust_data.get('OpenBalance', 0))
            invoices_after = cust_invoices_all[cust_invoices_all["Date"] > date_to_str]
            payments_after = cust_payments_all[cust_payments_all["Date"] > date_to_str]
            invoices_after_total = sum(to_decimal_safe(inv['Total']) for _, inv in invoices_after.iterrows())
            payments_after_total = sum(to_decimal_safe(pay['Amount']) for _, pay in payments_after.iterrows())
            closing_bal = current_balance - (invoices_after_total - payments_after_total)
            opening_bal = closing_bal - net_change

            st.markdown(f"""
            <div style='background: #F9FAFB; padding: 16px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #6366f1;'>
                <div style='display: flex; justify-content: space-between; align-items: center;'>
                    <span style='color: #374151; font-weight: 600; font-size: 16px;'>Net Change (Invoices: Rs {total_invoices:,.2f} - Payments: Rs {total_payments:,.2f})</span>
                    <span style='color: #6366f1; font-weight: 700; font-size: 20px;'>Rs {net_change:,.2f}</span>
                </div>
                <div style='display: flex; justify-content: space-between; align-items: center; margin-top: 8px;'>
                    <span style='color: #6B7280; font-weight: 600; font-size: 14px;'>Opening: Rs {opening_bal:,.2f}</span>
                    <span style='color: #6B7280; font-weight: 600; font-size: 14px;'>Closing: Rs {closing_bal:,.2f}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown("##### Transaction History")

            transactions = []
            for _, inv in cust_invoices.iterrows():
                transactions.append({
                    "Date": inv["Date"],
                    "Type": "Invoice",
                    "Reference": inv["InvoiceID"],
                    "Debit": f"Rs {to_decimal_safe(inv['Total']):,.2f}",
                    "Credit": "-",
                    "Status": inv["Status"]
                })
            for _, pay in cust_payments.iterrows():
                transactions.append({
                    "Date": pay["Date"],
                    "Type": "Payment",
                    "Reference": pay["PaymentID"],
                    "Debit": "-",
                    "Credit": f"Rs {to_decimal_safe(pay['Amount']):,.2f}",
                    "Status": pay["PaymentMethod"]
                })
            transactions.sort(key=lambda x: x["Date"])

            if transactions:
                display_df = pd.DataFrame(transactions)
                st.dataframe(display_df, hide_index=True, width="stretch")

            st.markdown("<br>", unsafe_allow_html=True)

            ledger_entries = [{"Date": "", "Description": "Opening Balance", "Debit": "", "Credit": "", "Balance": opening_bal}]
            for txn in transactions:
                ledger_entries.append({
                    "Date": txn["Date"],
                    "Description": f"{txn['Type']} {txn['Reference']}",
                    "Debit": txn["Debit"].replace("Rs ", "").replace(",", "") if txn["Debit"] != "-" else "",
                    "Credit": txn["Credit"].replace("Rs ", "").replace(",", "") if txn["Credit"] != "-" else "",
                    "Balance": ""
                })

            pdf_bytes = export_account_ledger_to_pdf(
                cust_id,
                cust_data['CustomerName'],
                date_from_str,
                date_to_str,
                ledger_entries,
                opening_bal,
                closing_bal,
                get_active_company_name()
            )

            st.download_button(
                "📥 Export Ledger (PDF)",
                pdf_bytes,
                file_name=f"customer_ledger_{cust_id}_{date_from.strftime('%Y%m%d')}_to_{date_to.strftime('%Y%m%d')}.pdf",
                mime="application/pdf"
            )
    
    with tab4:
        st.markdown("#### Vendor Ledger Report")
        st.markdown("<br>", unsafe_allow_html=True)

        vendors = load_table("vendors")
        bills = load_table("bills")
        vendor_payments = load_table("vendor_payments")

        if vendors.empty:
            st.info("ℹ️ No vendors available. Add vendors to generate ledger reports.")
        else:
            selected_vendor = st.selectbox(
                "Select Vendor",
                [f"{v['VendorID']} - {v['VendorName']}" for _, v in vendors.iterrows()],
                key="rpt_vend_ledger_select"
            )
            vend_id = selected_vendor.split(" - ")[0]
            vend_data = vendors[vendors["VendorID"] == vend_id].iloc[0]

            col1, col2 = st.columns(2)
            with col1:
                date_from = st.date_input("From Date", datetime.now() - timedelta(days=30), key="rpt_vend_ledger_from")
            with col2:
                date_to = st.date_input("To Date", datetime.now(), key="rpt_vend_ledger_to")

            date_from_str = date_from.strftime("%Y-%m-%d")
            date_to_str = date_to.strftime("%Y-%m-%d")

            st.divider()

            st.markdown(f"""
            <div style='background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); padding: 24px; border-radius: 12px; margin-bottom: 24px; color: white;'>
                <h3 style='margin: 0 0 16px 0; font-size: 22px;'>{vend_data['VendorName']}</h3>
                <div style='display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;'>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Email</p>
                        <p style='margin: 4px 0 0 0; font-size: 15px; font-weight: 600;'>{vend_data.get('Email', 'N/A')}</p>
                    </div>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Phone</p>
                        <p style='margin: 4px 0 0 0; font-size: 15px; font-weight: 600;'>{vend_data.get('Phone', 'N/A')}</p>
                    </div>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Period</p>
                        <p style='margin: 4px 0 0 0; font-size: 15px; font-weight: 600;'>{date_from.strftime('%d %b %Y')} - {date_to.strftime('%d %b %Y')}</p>
                    </div>
                    <div>
                        <p style='margin: 0; font-size: 12px; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px;'>Current Payable</p>
                        <p style='margin: 4px 0 0 0; font-size: 18px; font-weight: 700;'>Rs {to_decimal_safe(vend_data.get('OpenBalance', 0)):,.2f}</p>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            vend_bills_all = bills[
                (bills["VendorID"] == vend_id) &
                (bills["Status"].isin(["Posted", "Paid"]))
            ]
            vend_bills = vend_bills_all[
                (vend_bills_all["Date"] >= date_from_str) &
                (vend_bills_all["Date"] <= date_to_str)
            ]

            vend_payments_all = vendor_payments[vendor_payments["VendorID"] == vend_id]
            vend_payments = vend_payments_all[
                (vend_payments_all["Date"] >= date_from_str) &
                (vend_payments_all["Date"] <= date_to_str)
            ]

            if vend_bills.empty and vend_payments.empty:
                st.info("ℹ️ No transactions in this period")
                return

            total_bills = sum(to_decimal_safe(bill['Total']) for _, bill in vend_bills.iterrows())
            total_payments = sum(to_decimal_safe(pay['Amount']) for _, pay in vend_payments.iterrows())
            net_change = total_bills - total_payments

            current_payable = to_decimal_safe(vend_data.get('OpenBalance', 0))
            bills_after = vend_bills_all[vend_bills_all["Date"] > date_to_str]
            payments_after = vend_payments_all[vend_payments_all["Date"] > date_to_str]
            bills_after_total = sum(to_decimal_safe(bill['Total']) for _, bill in bills_after.iterrows())
            payments_after_total = sum(to_decimal_safe(pay['Amount']) for _, pay in payments_after.iterrows())
            closing_bal = current_payable - (bills_after_total - payments_after_total)
            opening_bal = closing_bal - net_change

            st.markdown(f"""
            <div style='background: #FEF3C7; padding: 16px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #f59e0b;'>
                <div style='display: flex; justify-content: space-between; align-items: center;'>
                    <span style='color: #92400E; font-weight: 600; font-size: 16px;'>Net Change (Bills: Rs {total_bills:,.2f} - Payments: Rs {total_payments:,.2f})</span>
                    <span style='color: #D97706; font-weight: 700; font-size: 20px;'>Rs {net_change:,.2f}</span>
                </div>
                <div style='display: flex; justify-content: space-between; align-items: center; margin-top: 8px;'>
                    <span style='color: #92400E; font-weight: 600; font-size: 14px;'>Opening: Rs {opening_bal:,.2f}</span>
                    <span style='color: #92400E; font-weight: 600; font-size: 14px;'>Closing: Rs {closing_bal:,.2f}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown("##### Transaction History")

            transactions = []
            for _, bill in vend_bills.iterrows():
                transactions.append({
                    "Date": bill["Date"],
                    "Type": "Bill",
                    "Reference": bill["BillID"],
                    "Debit": f"Rs {to_decimal_safe(bill['Total']):,.2f}",
                    "Credit": "-",
                    "Status": bill["Status"]
                })
            for _, pay in vend_payments.iterrows():
                transactions.append({
                    "Date": pay["Date"],
                    "Type": "Payment",
                    "Reference": pay["PaymentID"],
                    "Debit": "-",
                    "Credit": f"Rs {to_decimal_safe(pay['Amount']):,.2f}",
                    "Status": pay["PaymentMethod"]
                })
            transactions.sort(key=lambda x: x["Date"])

            if transactions:
                display_df = pd.DataFrame(transactions)
                st.dataframe(display_df, hide_index=True, width="stretch")

            st.markdown("<br>", unsafe_allow_html=True)

            ledger_entries = [{"Date": "", "Description": "Opening Balance", "Debit": "", "Credit": "", "Balance": opening_bal}]
            for txn in transactions:
                ledger_entries.append({
                    "Date": txn["Date"],
                    "Description": f"{txn['Type']} {txn['Reference']}",
                    "Debit": txn["Debit"].replace("Rs ", "").replace(",", "") if txn["Debit"] != "-" else "",
                    "Credit": txn["Credit"].replace("Rs ", "").replace(",", "") if txn["Credit"] != "-" else "",
                    "Balance": ""
                })

            pdf_bytes = export_account_ledger_to_pdf(
                vend_id,
                vend_data['VendorName'],
                date_from_str,
                date_to_str,
                ledger_entries,
                opening_bal,
                closing_bal,
                get_active_company_name()
            )

            st.download_button(
                "📥 Export Ledger (PDF)",
                pdf_bytes,
                file_name=f"vendor_ledger_{vend_id}_{date_from.strftime('%Y%m%d')}_to_{date_to.strftime('%Y%m%d')}.pdf",
                mime="application/pdf"
            )
    
    with tab5:
        st.markdown("#### 📦 Item-Wise Profit Analysis")
        st.markdown("<br>", unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            date_from = st.date_input("From Date", key="rpt_item_profit_from")
        with col2:
            date_to = st.date_input("To Date", datetime.now(), key="rpt_item_profit_to")
        
        st.divider()
        
        # Load data
        invoices = load_table("invoices")
        inventory = load_table("inventory")
        
        date_from_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")
        
        # Filter posted/paid invoices in date range
        posted_invoices = invoices[
            (invoices['Date'] >= date_from_str)
            & (invoices['Date'] <= date_to_str)
            & (invoices['Status'].isin(['Posted', 'Paid']))
        ]
        
        if posted_invoices.empty:
            st.info("📭 No posted invoices in this period")
        else:
            # Collect item-wise data
            item_stats = {}
            
            for _, invoice in posted_invoices.iterrows():
                items = get_invoice_line_items(invoice['InvoiceID'])
                for item in items:
                    item_name = item.get('item') or item.get('ItemName', 'Unknown')
                    item_id = item.get('item_id') or item.get('ItemID')
                    qty = int(item.get('qty', 0))
                    sell_price = to_decimal_safe(item.get('price', 0))
                    line_total = to_decimal_safe(item.get('total', sell_price * qty))
                    
                    # Get cost price from inventory
                    if item_id:
                        inv_item = inventory[inventory['ItemID'] == item_id]
                    else:
                        inv_item = inventory[inventory['ItemName'] == item_name]
                    
                    if not inv_item.empty:
                        cost_price = to_decimal_safe(inv_item.iloc[0]['CostPrice'])
                    else:
                        cost_price = Decimal('0.00')
                    
                    # Calculate totals
                    # Prefer stored line total to better reflect actual invoiced amount
                    revenue = line_total
                    cost = cost_price * qty
                    profit = revenue - cost
                    
                    # Accumulate stats
                    if item_name not in item_stats:
                        item_stats[item_name] = {
                            'qty': 0,
                            'revenue': Decimal('0.00'),
                            'cost': Decimal('0.00'),
                            'profit': Decimal('0.00')
                        }
                    
                    item_stats[item_name]['qty'] += qty
                    item_stats[item_name]['revenue'] += revenue
                    item_stats[item_name]['cost'] += cost
                    item_stats[item_name]['profit'] += profit
            
            if not item_stats:
                st.info("📭 No items found in posted invoices")
            else:
                # Convert to dataframe
                data = []
                for item_name, stats in item_stats.items():
                    profit_margin = (stats['profit'] / stats['revenue'] * 100) if stats['revenue'] > 0 else Decimal('0.00')
                    data.append({
                        'Item': item_name,
                        'Qty Sold': stats['qty'],
                        'Revenue': stats['revenue'],
                        'Cost': stats['cost'],
                        'Profit': stats['profit'],
                        'Margin %': profit_margin
                    })
                
                df = pd.DataFrame(data)
                df = df.sort_values('Profit', ascending=False)
                
                # Summary metrics
                total_revenue = sum(stats['revenue'] for stats in item_stats.values())
                total_cost = sum(stats['cost'] for stats in item_stats.values())
                total_profit = sum(stats['profit'] for stats in item_stats.values())
                overall_margin = (total_profit / total_revenue * 100) if total_revenue > 0 else Decimal('0.00')
                
                st.markdown("### Summary")
                col1, col2, col3, col4 = st.columns(4)
                
                with col1:
                    st.markdown(f"""
                    <div style='background: linear-gradient(135deg, #10b981 0%, #059669 100%); 
                                padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px rgba(16,185,129,0.25);'>
                        <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px;'>Total Revenue</p>
                        <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>Rs {total_revenue:,.2f}</h2>
                    </div>
                    """, unsafe_allow_html=True)
                
                with col2:
                    st.markdown(f"""
                    <div style='background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); 
                                padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px rgba(59,130,246,0.25);'>
                        <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px;'>Total Cost</p>
                        <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>Rs {total_cost:,.2f}</h2>
                    </div>
                    """, unsafe_allow_html=True)
                
                with col3:
                    st.markdown(f"""
                    <div style='background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); 
                                padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px rgba(245,158,11,0.25);'>
                        <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px;'>Total Profit</p>
                        <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>Rs {total_profit:,.2f}</h2>
                    </div>
                    """, unsafe_allow_html=True)
                
                with col4:
                    st.markdown(f"""
                    <div style='background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); 
                                padding: 24px; border-radius: 12px; box-shadow: 0 4px 12px rgba(139,92,246,0.25);'>
                        <p style='color: rgba(255,255,255,0.85); font-size: 13px; margin: 0; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px;'>Avg Margin</p>
                        <h2 style='color: white; margin: 12px 0 0 0; font-size: 28px; font-weight: 700;'>{overall_margin:.1f}%</h2>
                    </div>
                    """, unsafe_allow_html=True)
                
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown("### Item-Wise Details")
                
                # Display table with formatted values
                display_df = df.copy()
                display_df['Revenue'] = display_df['Revenue'].apply(lambda x: f"Rs {x:,.2f}")
                display_df['Cost'] = display_df['Cost'].apply(lambda x: f"Rs {x:,.2f}")
                display_df['Profit'] = display_df['Profit'].apply(lambda x: f"Rs {x:,.2f}")
                display_df['Margin %'] = display_df['Margin %'].apply(lambda x: f"{x:.1f}%")
                
                st.dataframe(display_df, hide_index=True, width="stretch", height=400)
                
                st.markdown("<br>", unsafe_allow_html=True)
                
                # Export to PDF
                if st.button("📥 Export to PDF", type="primary"):
                    try:
                        pdf = FPDF()
                        pdf.add_page()
                        pdf.set_font("Arial", "", 10)
                        max_y = 265
                        def print_chalan_table_header():
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_fill_color(240, 240, 240)
                            pdf.cell(20, 8, "Sr. No.", 1, 0, 'C', True)
                            pdf.cell(120, 8, "Item", 1, 0, 'L', True)
                            pdf.cell(40, 8, "Quantity", 1, 1, 'C', True)
                            pdf.set_font("Arial", "", 10)
                        print_chalan_table_header()
                        sr_no = 1
                        for item in items:
                            if pdf.get_y() > max_y:
                                pdf.add_page()
                                print_chalan_table_header()
                            pdf.cell(20, 7, str(sr_no), 1, 0, 'C')
                            pdf.cell(120, 7, str(item.get('item', '')), 1, 0, 'L')
                            pdf.cell(40, 7, f"{item.get('qty', '')} units", 1, 1, 'C')
                            sr_no += 1
                        # Summary
                        pdf.set_font("Arial", "B", 12)
                        pdf.cell(0, 8, "Summary", 0, 1)
                        pdf.set_font("Arial", "", 10)
                        pdf.cell(0, 6, f"Total Revenue: Rs {total_revenue:,.2f}", 0, 1)
                        pdf.cell(0, 6, f"Total Cost: Rs {total_cost:,.2f}", 0, 1)
                        pdf.cell(0, 6, f"Total Profit: Rs {total_profit:,.2f}", 0, 1)
                        pdf.cell(0, 6, f"Average Margin: {overall_margin:.1f}%", 0, 1)
                        pdf.ln(5)
                        
                        # Table header
                        pdf.set_font("Arial", "B", 9)
                        pdf.set_fill_color(200, 200, 200)
                        pdf.cell(60, 7, "Item", 1, 0, 'L', True)
                        pdf.cell(25, 7, "Qty Sold", 1, 0, 'C', True)
                        pdf.cell(30, 7, "Revenue", 1, 0, 'R', True)
                        pdf.cell(30, 7, "Cost", 1, 0, 'R', True)
                        pdf.cell(30, 7, "Profit", 1, 0, 'R', True)
                        pdf.cell(20, 7, "Margin%", 1, 1, 'C', True)
                        
                        # Table data
                        pdf.set_font("Arial", "", 8)
                        for _, row in df.iterrows():
                            pdf.cell(60, 6, str(row['Item'])[:30], 1, 0, 'L')
                            pdf.cell(25, 6, str(row['Qty Sold']), 1, 0, 'C')
                            pdf.cell(30, 6, f"{row['Revenue']:,.2f}", 1, 0, 'R')
                            pdf.cell(30, 6, f"{row['Cost']:,.2f}", 1, 0, 'R')
                            pdf.cell(30, 6, f"{row['Profit']:,.2f}", 1, 0, 'R')
                            pdf.cell(20, 6, f"{row['Margin %']:.1f}%", 1, 1, 'C')
                        
                        pdf_output = BytesIO()
                        pdf_output.write(pdf.output(dest='S').encode('latin-1'))
                        pdf_output.seek(0)
                        
                        st.download_button(
                            "⬇️ Download PDF",
                            pdf_output,
                            file_name=f"item_wise_profit_{date_from.strftime('%Y%m%d')}_to_{date_to.strftime('%Y%m%d')}.pdf",
                            mime="application/pdf"
                        )
                    except Exception as e:
                        st.error(f"Error generating PDF: {e}")

    with tab6:
        st.markdown(f"#### {t('cash_flow')}" if t('cash_flow') != 'cash_flow' else "#### Cash Flow")
        st.caption("Based on received/sent payments (not invoices/bills)")
        st.markdown("<br>", unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        with col1:
            date_from = st.date_input(t('from_date'), datetime.now() - timedelta(days=30), key="rpt_cashflow_from")
        with col2:
            date_to = st.date_input(t('to_date'), datetime.now(), key="rpt_cashflow_to")

        date_from_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")

        customer_payments = load_table("customer_payments")
        vendor_payments = load_table("vendor_payments")
        customers = load_table("customers")
        vendors = load_table("vendors")

        cust_map = {}
        if not customers.empty:
            cust_map = dict(zip(customers["CustomerID"].astype(str), customers["CustomerName"].astype(str)))
        vend_map = {}
        if not vendors.empty:
            vend_map = dict(zip(vendors["VendorID"].astype(str), vendors["VendorName"].astype(str)))

        cp_range = customer_payments
        if not customer_payments.empty and "Date" in customer_payments.columns:
            cp_range = customer_payments[(customer_payments["Date"] >= date_from_str) & (customer_payments["Date"] <= date_to_str)]

        vp_range = vendor_payments
        if not vendor_payments.empty and "Date" in vendor_payments.columns:
            vp_range = vendor_payments[(vendor_payments["Date"] >= date_from_str) & (vendor_payments["Date"] <= date_to_str)]

        cash_in = sum(to_decimal_safe(p.get('Amount', 0)) for _, p in cp_range.iterrows()) if not cp_range.empty else Decimal("0.00")
        cash_out = sum(to_decimal_safe(p.get('Amount', 0)) for _, p in vp_range.iterrows()) if not vp_range.empty else Decimal("0.00")
        net_cash = cash_in - cash_out

        c1, c2, c3 = st.columns(3)
        c1.metric(t('cash_in'), f"Rs {cash_in:,.2f}")
        c2.metric(t('cash_out'), f"Rs {cash_out:,.2f}")
        c3.metric(t('net_cash_flow'), f"Rs {net_cash:,.2f}")

        st.divider()

        if cp_range.empty and vp_range.empty:
            st.info("No payments in this period")
            return

        rows = []
        if not cp_range.empty:
            for _, pay in cp_range.iterrows():
                cust_id = str(pay.get("CustomerID", ""))
                rows.append({
                    "Date": str(pay.get("Date", "")),
                    "Type": "Received",
                    "Party": cust_map.get(cust_id, cust_id),
                    "Amount": f"Rs {to_decimal_safe(pay.get('Amount', 0)):,.2f}",
                    "Method": str(pay.get("PaymentMethod", "")),
                    "PaymentID": str(pay.get("PaymentID", "")),
                    "Notes": str(pay.get("Notes", "")) if str(pay.get("Notes", "")).strip() else "-",
                })

        if not vp_range.empty:
            for _, pay in vp_range.iterrows():
                vend_id = str(pay.get("VendorID", ""))
                rows.append({
                    "Date": str(pay.get("Date", "")),
                    "Type": "Sent",
                    "Party": vend_map.get(vend_id, vend_id),
                    "Amount": f"Rs {to_decimal_safe(pay.get('Amount', 0)):,.2f}",
                    "Method": str(pay.get("PaymentMethod", "")),
                    "PaymentID": str(pay.get("PaymentID", "")),
                    "Notes": str(pay.get("Notes", "")) if str(pay.get("Notes", "")).strip() else "-",
                })

        df = pd.DataFrame(rows)
        if not df.empty and "Date" in df.columns:
            df = df.sort_values(by=["Date", "PaymentID"], ascending=[True, True])

        st.markdown("##### Transactions")
        st.dataframe(df, hide_index=True, width="stretch")


def show_orders():
    """Orders module: create pending orders and mark them as filled."""
    show_orders_page(get_db=get_db, load_table=load_table)

def show_admin():
    """Admin section."""
    require_admin()
    
    st.title("⚙️ Admin Panel")
    
    tab1, tab2, tab3, tab4 = st.tabs(["Customers", "Vendors", "Manage Users", "Settings"])
    
    with tab1:
        st.markdown("### 👥 Customers")

# ==================== MAIN APP ====================
def main():
    """Main application."""
    st.set_page_config(
        page_title="Abaseen",
        layout="wide",
        page_icon="📊",
        initial_sidebar_state="collapsed",
    )
    
    # Initialize session state first
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "username" not in st.session_state:
        st.session_state.username = None
    if "role" not in st.session_state:
        st.session_state.role = None
    if "nav_choice" not in st.session_state:
        st.session_state.nav_choice = "Dashboard"
    if "auto_backup_checked" not in st.session_state:
        st.session_state.auto_backup_checked = False
    if 'language' not in st.session_state:
        st.session_state.language = 'English'

    # Multi-company bootstrap (safe to call on reruns)
    bootstrap_company_registry()
    
    # Check if Urdu/Arabic is selected for proper fonts
    is_urdu = st.session_state.language == 'اردو (Urdu)'
    is_arabic = st.session_state.language == 'العربية (Arabic)'
    base_font_size = "18px" if is_urdu else "14px"
    button_font_size = "18px" if is_urdu else "14px"
    label_font_size = "17px" if is_urdu else "14px"
    if is_urdu:
        font_family = '"Noto Nastaliq Urdu", "Jameel Noori Nastaleeq", "Mehr Nastaliq Web", "Segoe UI", Arial, sans-serif'
    elif is_arabic:
        font_family = '"Noto Naskh Arabic", "Cairo", "Segoe UI", Arial, sans-serif'
    else:
        font_family = '"Segoe UI", "Helvetica Neue", Arial, sans-serif'
    
    # Professional styling with dynamic Urdu font sizing
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Noto+Nastaliq+Urdu:wght@400;500;600;700&family=Noto+Naskh+Arabic:wght@400;500;600;700&family=Cairo:wght@400;500;600;700&display=swap');
        
        :root {{
            --bg: #F5F7FA;
            --card: #ffffff;
            --primary: #2CA01C;
            --text: #1F2937;
            --muted: #6B7280;
            --border: #E5E7EB;
        }}
        html, body, .stApp {{
            background: var(--bg);
            color: var(--text);
            font-family: {font_family};
            font-size: {base_font_size};
        }}
        
        /* Apply font to text elements without breaking colors */
        p, h1, h2, h3, h4, h5, h6, span, div, label, li, td, th {{
            font-family: {font_family};
        }}
        
        /* Streamlit Buttons */
        .stButton button {{
            font-size: {button_font_size} !important;
            font-weight: 600 !important;
            font-family: {font_family} !important;
        }}
        
        /* Form Labels */
        .stSelectbox label, .stTextInput label, .stTextArea label, .stDateInput label, .stNumberInput label, .stCheckbox label, .stRadio label {{
            font-size: {label_font_size} !important;
            font-weight: 600 !important;
            font-family: {font_family} !important;
        }}
        
        /* Select boxes */
        .stSelectbox div[data-baseweb="select"] {{
            font-size: {base_font_size} !important;
            font-family: {font_family} !important;
        }}
        .stSelectbox div[data-baseweb="select"] span {{
            font-family: {font_family} !important;
        }}
        
        /* Input fields */
        .stTextInput input, .stTextArea textarea, .stNumberInput input {{
            font-size: {base_font_size} !important;
            font-family: {font_family} !important;
        }}
        
        /* Markdown content */
        .stMarkdown {{
            font-size: {base_font_size} !important;
            font-family: {font_family} !important;
        }}
        .stMarkdown p, .stMarkdown span, .stMarkdown div {{
            font-family: {font_family} !important;
        }}
        
        /* DataFrames */
        .stDataFrame {{
            font-size: {base_font_size} !important;
        }}
        .stDataFrame table {{
            font-family: {font_family} !important;
        }}
        
        /* Metrics */
        .stMetric {{
            font-family: {font_family} !important;
        }}
        .stMetric label {{
            font-size: {label_font_size} !important;
            font-family: {font_family} !important;
        }}
        .stMetric [data-testid="stMetricValue"] {{
            font-size: 24px !important;
            font-family: {font_family} !important;
        }}
        
        /* Sidebar */
        div[data-testid="stSidebarNav"] {{
            font-size: {base_font_size} !important;
            font-family: {font_family} !important;
        }}
        section[data-testid="stSidebar"] {{
            background: #ffffff;
            border-right: 1px solid var(--border);
        }}
        
        /* Tabs */
        .stTabs [data-baseweb="tab-list"] {{
            overflow-x: auto;
            overflow-y: hidden;
            flex-wrap: nowrap;
        }}
        .stTabs [data-baseweb="tab-list"] button {{
            font-size: {base_font_size} !important;
            font-family: {font_family} !important;
            white-space: nowrap;
            flex: 0 0 auto;
        }}
        
        /* Expanders */
        .streamlit-expanderHeader {{
            font-size: {base_font_size} !important;
            font-family: {font_family} !important;
        }}
        
        /* Alerts */
        .stAlert {{
            font-size: {base_font_size} !important;
            font-family: {font_family} !important;
        }}
        
        /* Custom classes */
        .card {{ 
            background: var(--card); 
            border-radius: 10px; 
            padding: 18px; 
            border: 1px solid var(--border); 
        }}
        .sidebar-title {{ 
            font-weight: 800; 
            font-size: {label_font_size}; 
            margin-bottom: 12px; 
            color: var(--text); 
            font-family: {font_family}; 
        }}
        .nav-group {{ 
            margin-bottom: 12px; 
        }}
        .nav-label {{ 
            font-size: {label_font_size}; 
            font-weight: 700; 
            color: var(--muted); 
            text-transform: uppercase; 
            letter-spacing: 0.4px; 
            margin-bottom: 6px; 
            font-family: {font_family}; 
        }}
        .footer {{ 
            color: var(--muted); 
            font-size: {base_font_size}; 
            margin-top: 12px; 
            text-align: center; 
            font-family: {font_family}; 
        }}

        /* Mobile tweaks */
        @media (max-width: 768px) {{
            .block-container {{
                padding-left: 0.9rem;
                padding-right: 0.9rem;
            }}
            /* Make columns wrap on small screens so KPI cards don't get squeezed/joined */
            div[data-testid="stHorizontalBlock"] {{
                flex-wrap: wrap !important;
                gap: 1rem !important;
                row-gap: 1rem !important;
                column-gap: 1rem !important;
            }}
            div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{
                /* Streamlit sets inline flex-basis; override for better mobile stacking */
                flex: 1 1 320px !important;
                min-width: 260px !important;
                padding-left: 0.4rem !important;
                padding-right: 0.4rem !important;
            }}
            div[data-testid="column"] > div {{
                margin-bottom: 1rem;
            }}

            /* Your KPI cards are mostly custom HTML with a gradient background. */
            /* Add spacing so cards don't look joined on small screens. */
            .stMarkdown div[style*="linear-gradient"] {{
                margin-bottom: 1rem !important;
            }}
            .stDataFrame {{
                overflow-x: auto;
            }}
            .stMetric [data-testid="stMetricValue"] {{
                font-size: 18px !important;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    
    # Perform auto-backup check once per session (after authentication)
    if st.session_state.authenticated and not st.session_state.auto_backup_checked:
        st.session_state.auto_backup_checked = True
        # Run auto-backup in background (silent)
        try:
            perform_auto_backup()
        except Exception:
            logging.exception("Auto-backup raised an exception (ignored)")
    
    # Login
    if not st.session_state.authenticated:
        # Enterprise-grade login page with gradient background using Streamlit native features
        st.markdown("""
        <style>
        [data-testid="stAppViewContainer"] {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        </style>
        """, unsafe_allow_html=True)
        
        # Company selector (top-left)
        companies = list_companies(active_only=True)
        if not companies:
            st.error("No companies found. Please contact admin.")
            st.stop()

        allowed_company_names = [COMPANY_NAME, SECOND_COMPANY_NAME]
        companies_by_name = {c["name"]: c for c in companies if c.get("name") in allowed_company_names}
        ordered_company_names = [n for n in allowed_company_names if n in companies_by_name]
        if len(ordered_company_names) != len(allowed_company_names):
            missing = [n for n in allowed_company_names if n not in companies_by_name]
            st.error(f"Missing company registry entries: {', '.join(missing)}")
            st.stop()

        default_company_name = st.session_state.get("company_name")
        if default_company_name in ordered_company_names:
            default_index = ordered_company_names.index(default_company_name)
        else:
            default_index = 0

        top_left, top_spacer = st.columns([1, 4])
        with top_left:
            selected_company_name = st.selectbox(
                "Company",
                ordered_company_names,
                index=default_index,
                key="company_selector",
            )

        selected_company = companies_by_name[selected_company_name]
        set_active_company(selected_company)

        # Center the login form
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            # Logo above the card (changes with selected company)
            logo_path = get_company_logo_path(selected_company_name)
            if logo_path.exists():
                col_a, col_b, col_c = st.columns([1, 2, 1])
                with col_b:
                    st.image(str(logo_path), width="stretch")
            
            # Title
            # Use English for login page (before user is authenticated)
            st.markdown(f"""
            <div style='text-align: center; margin: 24px 0 32px 0;'>
                <h1 style='color: #FFFFFF; font-size: 32px; font-weight: 800; margin-bottom: 8px; text-shadow: 0 2px 4px rgba(0,0,0,0.1);'>{t('business_management_system', 'English')}</h1>
            </div>
            """, unsafe_allow_html=True)
            
            # White container for login form only
            with st.container(border=True):
                st.markdown("""
                <style>
                div[data-testid="stVerticalBlock"]:has(> div > button[kind="primary"]) {
                    background-color: white;
                    padding: 32px;
                    border-radius: 12px;
                }
                </style>
                """, unsafe_allow_html=True)
                
                # Login form
                with st.form("login"):
                    username = st.text_input(t('username', 'English'), placeholder=t('enter_username', 'English'))
                    password = st.text_input(t('password', 'English'), type="password", placeholder=t('enter_password', 'English'))
                    
                    st.markdown("<br>", unsafe_allow_html=True)
                    
                    if st.form_submit_button(f"🔐 {t('sign_in', 'English')}", width="stretch", type="primary"):
                        # Ensure schema exists for the selected company DB
                        try:
                            init_db(db_path=get_active_db_path())
                        except Exception:
                            logging.exception("Failed initializing selected company database")
                            st.error("❌ Company database could not be initialized")
                            st.stop()

                        with get_db() as conn:
                            user = conn.execute(
                                "SELECT PasswordHash, Role FROM users WHERE Username = ?", 
                                (username,)
                            ).fetchone()
                        
                        if user and verify_password(password, user[0]):
                            st.session_state.authenticated = True
                            st.session_state.username = username
                            st.session_state.role = user[1]
                            st.success(f"✅ {t('login_successful', 'English')}")
                            st.rerun()
                        else:
                            st.error(f"❌ {t('invalid_credentials', 'English')}")

        st.stop()
    
    # Navigation sidebar - simplified with just section buttons
    is_admin = st.session_state.role == "admin"
    
    with st.sidebar:
        logo_path = get_company_logo_path(get_active_company_name())
        if logo_path.exists():
            st.image(str(logo_path), width=140)
        
        # Removed company name from sidebar as requested
        st.caption(f"🏢 {get_active_company_name()}")
        st.caption(f"👤 {st.session_state.username}")

        st.markdown("""
            <style>
            .stButton>button {
                border-radius: 8px !important;
                font-weight: 600 !important;
                font-size: 1em !important;
                margin-bottom: 0.5em !important;
                width: 100% !important;
                min-width: 180px !important;
                max-width: 220px !important;
                padding: 0.75em 0 !important;
                transition: background 0.2s, color 0.2s;
                border: none !important;
                box-shadow: 0 2px 8px rgba(191,161,74,0.08);
                display: block !important;
                margin-left: auto !important;
                margin-right: auto !important;
                text-align: center !important;
            }
            /* Unique colors for each button by order in sidebar */
            .stButton:nth-of-type(1) button { background: linear-gradient(90deg, #f7971e, #ffd200); color: #222; }
            .stButton:nth-of-type(2) button { background: linear-gradient(90deg, #21d4fd, #b721ff); color: #fff; }
            .stButton:nth-of-type(3) button { background: linear-gradient(90deg, #43cea2, #185a9d); color: #fff; }
            .stButton:nth-of-type(4) button { background: linear-gradient(90deg, #ff512f, #dd2476); color: #fff; }
            .stButton:nth-of-type(5) button { background: linear-gradient(90deg, #f7971e, #ffd200); color: #222; }
            .stButton:nth-of-type(6) button { background: linear-gradient(90deg, #21d4fd, #b721ff); color: #fff; }
            .stButton:nth-of-type(7) button { background: linear-gradient(90deg, #43cea2, #185a9d); color: #fff; }
            .stButton:nth-of-type(8) button { background: linear-gradient(90deg, #ff512f, #dd2476); color: #fff; }
            .stButton:nth-of-type(9) button { background: linear-gradient(90deg, #f7971e, #ffd200); color: #222; }
            .stButton:nth-of-type(10) button { background: linear-gradient(90deg, #21d4fd, #b721ff); color: #fff; }
            .stButton:nth-of-type(11) button { background: linear-gradient(90deg, #43cea2, #185a9d); color: #fff; }
            .stButton:nth-of-type(12) button { background: linear-gradient(90deg, #ff512f, #dd2476); color: #fff; }
            .stButton>button:hover {
                filter: brightness(1.1);
                box-shadow: 0 2px 8px rgba(191,161,74,0.15);
            }
            </style>
        """, unsafe_allow_html=True)

        # Main navigation - just section buttons
        if st.button(f"📊 {t('dashboard')}", key="nav_dashboard", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Dashboard"
        if st.button(f"📈 {t('reports')}", key="nav_reports", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Reports"

        st.divider()

        if st.button(f"💰 {t('sales')}", key="nav_sales", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Create Estimate"
        if st.button(f"📦 {t('purchases')}", key="nav_purchases", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Add Bill"
        if st.button(f"📋 {t('orders')}", key="nav_orders", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Orders"
        if st.button(f"📊 {t('inventory')}", key="nav_inventory", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Stock List"
        if st.button(f"💸 {t('expenses')}", key="nav_expenses", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Add Expense"
        if st.button(f"💼 {t('loans')}", key="nav_loans", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Loans"
        if st.button(f"💵 {t('payments')}", key="nav_payments", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Payments"

        st.divider()
        
        # Customers and Vendors
        if st.button(f"👥 {t('customers')}", key="nav_customers", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Customers"
        if st.button(f"🏢 {t('vendors')}", key="nav_vendors", width="stretch", type="secondary"):
            st.session_state.nav_choice = "Vendors"
        
        # Admin panel
        if is_admin:
            st.divider()
            st.markdown(f"<div class='nav-label'>⚙️ {t('admin')}</div>", unsafe_allow_html=True)
            if st.button(f"  💾 {t('backup_export')}", width="stretch"):
                st.session_state.nav_choice = "Backup"
        
        # Logout at the bottom
        st.markdown("<div style='flex-grow: 1;'></div>", unsafe_allow_html=True)
        st.divider()
        if st.button(f"🚪 {t('logout')}", width="stretch", type="secondary", key="btn_logout"):
            st.session_state.clear()
            st.rerun()
        
        # Footer
        st.markdown(f"<div class='footer'>{get_active_company_name()} • v1.0</div>", unsafe_allow_html=True)
        st.markdown("<div class='footer'>by Abdul Hanan</div>", unsafe_allow_html=True)
        st.markdown("<div class='footer'>03125817185</div>", unsafe_allow_html=True)
    
    # Route to pages based on nav_choice
    choice = st.session_state.nav_choice
    
    if choice == "Dashboard":
        show_dashboard()
    elif choice == "Reports":
        show_reports()
    elif choice in ["Create Estimate", "Convert to Invoice", "Manage Invoices"]:
        show_sales()
    elif choice in ["Add Bill", "Vendor Ledger"]:
        show_purchases()
    elif choice == "Orders":
        show_orders()
    elif choice in ["Stock List", "Low Stock Alert"]:
        show_inventory()
    elif choice in ["Add Expense", "Expense List"]:
        show_expenses()
    elif choice == "Loans":
        show_loans()
    elif choice == "Payments":
        show_payments()
    elif choice == "Customers":
        if is_admin:
            show_manage_customers()
    elif choice == "Vendors":
        if is_admin:
            show_manage_vendors()
    elif choice == "Backup":
        if is_admin:
            show_backup()

def show_manage_customers():
    """Manage customers (admin only)."""
    st.title("👥 Customers")
    
    customers = load_table("customers")
    
    with st.expander("➕ Add New Customer", expanded=False):
        with st.form("add_customer"):
            name = st.text_input("Customer Name")
            email = st.text_input("Email")
            phone = st.text_input("Phone")
            address = st.text_area("Address")
            open_balance = st.number_input("Opening Balance (Rs)", 0.0, help="Starting balance for this customer")
            
            if st.form_submit_button("✅ Add Customer"):
                if not name:
                    st.error("Name required")
                else:
                    cust_id = get_next_id("CUST", "customers", "CustomerID")
                    new_cust = {
                        "CustomerID": cust_id,
                        "CustomerName": name,
                        "Email": email,
                        "Phone": phone,
                        "Address": address,
                        "OpenBalance": open_balance,
                        "Active": "Yes"
                    }
                    customers = pd.concat([customers, pd.DataFrame([new_cust])], ignore_index=True)
                    save_table("customers", customers)
                    st.success("✅ Customer added")
                    st.rerun()
    
    if not customers.empty:
        st.divider()
        st.subheader("All Customers")
        
        for _, cust in customers.iterrows():
            with st.expander(f"👤 {cust['CustomerName']} - {cust['Phone']} - Balance: Rs {to_decimal_safe(cust.get('OpenBalance', 0)):,.2f}"):
                col1, col2, col3 = st.columns(3)
                col1.write(f"**Email:** {cust['Email']}")
                col2.write(f"**Phone:** {cust['Phone']}")
                col3.metric("Open Balance", f"Rs {to_decimal_safe(cust.get('OpenBalance', 0)):,.2f}")
                st.write(f"**Address:** {cust['Address']}")
                
                st.divider()
                
                # Edit form
                with st.form(f"edit_cust_{cust['CustomerID']}"):
                    st.subheader("✏️ Edit Customer")
                    new_name = st.text_input("Name", value=cust['CustomerName'])
                    new_email = st.text_input("Email", value=cust['Email'])
                    new_phone = st.text_input("Phone", value=cust['Phone'])
                    new_address = st.text_area("Address", value=cust['Address'])
                    new_open_balance = st.number_input("Open Balance (Rs)", value=float(to_decimal_safe(cust.get('OpenBalance', 0))), help="Adjust customer's opening balance")
                    
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.form_submit_button("💾 Save Changes", width="stretch"):
                            updates = {
                                'CustomerName': new_name,
                                'Email': new_email,
                                'Phone': new_phone,
                                'Address': new_address,
                                'OpenBalance': new_open_balance
                            }
                            update_record('customers', 'CustomerID', cust['CustomerID'], updates)
                            st.success("✅ Customer updated")
                            st.rerun()
                    
                    with col2:
                        if st.form_submit_button("🗑️ Delete Customer", width="stretch"):
                            if can_delete_customer(cust['CustomerID']):
                                soft_delete_record('customers', 'CustomerID', cust['CustomerID'])
                                st.success("✅ Customer deleted")
                                st.rerun()
                            else:
                                st.error("❌ Cannot delete: Customer has invoices or estimates")

def show_manage_vendors():
    """Manage vendors (admin only)."""
    st.title("🏢 Vendors")
    
    vendors = load_table("vendors")
    
    col1, col2 = st.columns([3, 1])
    with col1:
        st.subheader("Add New Vendor")
    
    with st.form("add_vendor"):
        name = st.text_input("Vendor Name")
        email = st.text_input("Email")
        phone = st.text_input("Phone")
        address = st.text_area("Address")
        open_balance = st.number_input("Opening Balance (Rs)", 0.0, help="Starting balance for this vendor")
        
        if st.form_submit_button("✅ Add Vendor"):
            if not name:
                st.error("Name required")
            else:
                # Prevent duplicate vendor names (case-insensitive)
                try:
                    exists = False
                    if not vendors.empty:
                        exists = (vendors['VendorName'].str.strip().str.lower() == str(name).strip().lower()).any()
                except Exception:
                    exists = False

                if exists:
                    st.error("A vendor with this name already exists. Please choose a different name.")
                else:
                    vend_id = get_next_id("VEND", "vendors", "VendorID")
                    new_vend = {
                        "VendorID": vend_id,
                        "VendorName": name,
                        "Email": email,
                        "Phone": phone,
                        "Address": address,
                        "OpenBalance": open_balance,
                        "Active": "Yes"
                    }
                    vendors = pd.concat([vendors, pd.DataFrame([new_vend])], ignore_index=True)
                    save_table("vendors", vendors)
                    st.success("✅ Vendor added")
                    st.rerun()
    
    if not vendors.empty:
        st.divider()
        st.subheader("All Vendors")
        
        for _, vend in vendors.iterrows():
            with st.expander(f"🏢 {vend['VendorName']} - {vend['Phone']} - Balance: Rs {to_decimal_safe(vend.get('OpenBalance', 0)):,.2f}"):
                col1, col2, col3 = st.columns(3)
                col1.write(f"**Email:** {vend['Email']}")
                col2.write(f"**Phone:** {vend['Phone']}")
                col3.metric("Open Balance", f"Rs {to_decimal_safe(vend.get('OpenBalance', 0)):,.2f}")
                st.write(f"**Address:** {vend['Address']}")
                
                st.divider()
                
                # Edit form
                with st.form(f"edit_vend_{vend['VendorID']}"):
                    st.subheader("✏️ Edit Vendor")
                    new_name = st.text_input("Name", value=vend['VendorName'])
                    new_email = st.text_input("Email", value=vend['Email'])
                    new_phone = st.text_input("Phone", value=vend['Phone'])
                    new_address = st.text_area("Address", value=vend['Address'])
                    new_open_balance = st.number_input("Open Balance (Rs)", value=float(to_decimal_safe(vend.get('OpenBalance', 0))), help="Adjust vendor's opening balance")
                    
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.form_submit_button("💾 Save Changes", width="stretch"):
                            updates = {
                                'VendorName': new_name,
                                'Email': new_email,
                                'Phone': new_phone,
                                'Address': new_address,
                                'OpenBalance': new_open_balance
                            }
                            update_record('vendors', 'VendorID', vend['VendorID'], updates)
                            st.success("✅ Vendor updated")
                            st.rerun()
                    
                    with col2:
                        if st.form_submit_button("🗑️ Delete Vendor", width="stretch"):
                            if can_delete_vendor(vend['VendorID']):
                                soft_delete_record('vendors', 'VendorID', vend['VendorID'])
                                st.success("✅ Vendor deleted")
                                st.rerun()
                            else:
                                st.error("❌ Cannot delete: Vendor has bills")

def show_loans():
    """Manage loan parties and view balances/transactions."""
    st.title(f"💼 {t('loans')}")

    parties = load_table("loan_parties")

    # Add new party
    with st.expander(f"➕ {t('add')} {t('loan_party')}", expanded=False):
        with st.form("add_loan_party"):
            name = st.text_input(t('party_name'))
            email = st.text_input("Email")
            phone = st.text_input("Phone")
            address = st.text_area("Address")
            opening_balance = st.number_input("Opening Balance (Rs)", 0.0)

            if st.form_submit_button("✅ Add Party"):
                if not name:
                    st.error("Name required")
                else:
                    # Prevent duplicate PartyName (unique constraint)
                    try:
                        exists = False
                        if not parties.empty:
                            exists = (parties['PartyName'].str.lower() == str(name).strip().lower()).any()
                    except Exception:
                        exists = False

                    if exists:
                        st.error("A loan party with this name already exists. Please choose a different name.")
                    else:
                        party_id = get_next_id("LPY", "loan_parties", "PartyID")
                        new_party = {
                            "PartyID": party_id,
                            "PartyName": name,
                            "Email": email,
                            "Phone": phone,
                            "Address": address,
                            "Balance": opening_balance,
                            "Active": "Yes"
                        }
                        parties = pd.concat([parties, pd.DataFrame([new_party])], ignore_index=True)
                        save_table("loan_parties", parties)
                        st.success("✅ Loan party added")
                        st.rerun()

    if parties.empty:
        st.info("No loan parties yet. Add one to start tracking loans.")
        return

    st.divider()

    # Build table view with Party, Balance and Type (determine Type from transactions)
    txs = load_table('loan_transactions')
    display = parties[['PartyID', 'PartyName', 'Balance']].copy()

    def compute_type(pid, bal):
        try:
            if txs is None or txs.empty:
                # Fallback to balance sign
                bval = to_decimal_safe(bal)
                if bval > 0:
                    return 'Received'
                elif bval < 0:
                    return 'Given'
                else:
                    return 'Settled'

            part_txs = txs[txs['PartyID'] == pid]
            received = Decimal('0.00')
            given = Decimal('0.00')
            for _, r in part_txs.iterrows():
                amt = to_decimal_safe(r.get('Amount', 0))
                dirv = str(r.get('Direction', '')).lower()
                if dirv == 'received':
                    received += amt
                elif dirv == 'given':
                    given += amt

            # Determine type based on current balance, not transaction comparison
            current_balance = to_decimal_safe(bal)
            if current_balance > 0:
                return 'Received'
            elif current_balance < 0:
                return 'Given'
            else:
                return 'Settled'
        except Exception:
            return 'Unknown'

    display['Type'] = display.apply(lambda row: compute_type(row['PartyID'], row['Balance']), axis=1)
    display['Balance'] = display['Balance'].apply(lambda x: f"Rs {to_decimal_safe(x):,.2f}")
    display = display.rename(columns={'PartyName': 'Party'})

    st.markdown("### Loan Parties")
    st.dataframe(display[['Party', 'Balance', 'Type']], hide_index=True, width='stretch')

    st.markdown("---")
    # Select a party to view transactions and delete
    party_select = st.selectbox("Select Party to view details", [f"{r['PartyID']} - {r['PartyName']}" for _, r in parties.iterrows()])
    sel_id = party_select.split(' - ')[0]
    sel_party = parties[parties['PartyID'] == sel_id].iloc[0]

    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown(f"**{sel_party['PartyName']}**")
        st.write(f"Email: {sel_party.get('Email','')}")
        st.write(f"Phone: {sel_party.get('Phone','')}")
        st.write(f"Address: {sel_party.get('Address','')}")
    with col2:
        st.metric("Balance", f"Rs {to_decimal_safe(sel_party.get('Balance',0)):,.2f}")
    
    # Per-party type setter (Received / Given / Settled) - changes balance sign if needed
    current_type = compute_type(sel_id, sel_party['Balance'])
    type_options = ["Received", "Given", "Settled"]
    try:
        default_index = type_options.index(current_type) if current_type in type_options else 0
    except Exception:
        default_index = 0

    new_type = st.selectbox("Set Type", type_options, index=default_index)
    if st.button("Update Type", key=f"update_type_{sel_id}"):
        bal = to_decimal_safe(sel_party.get('Balance', 0))
        new_bal = None
        if new_type == 'Given' and bal > 0:
            new_bal = float(-abs(bal))
        elif new_type == 'Received' and bal < 0:
            new_bal = float(abs(bal))
        elif new_type == 'Settled':
            new_bal = 0.0

        if new_bal is not None:
            update_record('loan_parties', 'PartyID', sel_id, {'Balance': new_bal})
            st.success(f"Type updated to {new_type} and balance adjusted")
            st.rerun()
        else:
            st.info("No balance sign change required for selected type")

    st.markdown("#### Transactions")
    txs = load_table('loan_transactions')
    party_txs = txs[txs['PartyID'] == sel_id] if not txs.empty else pd.DataFrame()
    if party_txs.empty:
        st.info("No transactions for this party")
    else:
        party_txs = party_txs[['TransactionID','Date','Direction','Amount','Notes']].copy()
        party_txs['Amount'] = party_txs['Amount'].apply(lambda x: f"Rs {to_decimal_safe(x):,.2f}")
        st.dataframe(party_txs.sort_values('Date', ascending=False), hide_index=True, width='stretch')

    # Deletion
    if st.button("🗑️ Delete Party", key=f"delete_party_{sel_id}"):
        bal = to_decimal_safe(sel_party.get('Balance',0))
        if bal == 0:
            soft_delete_record('loan_parties', 'PartyID', sel_id)
            st.success("✅ Party deleted")
            st.rerun()
        else:
            st.error("❌ Cannot delete: Party balance is not zero. Set balance to 0 before deleting.")


def show_backup():
    """Backup and export system (admin only)."""
    st.title("💾 Backup & Export")
    
    # Auto-Backup Section at the top
    st.markdown("### 🔄 Automatic Daily Backup")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        # Get current auto-backup status
        auto_backup_enabled = get_backup_config("auto_backup_enabled") == "Yes"
        last_backup = get_backup_config("last_backup_date")
        
        if auto_backup_enabled:
            st.success("✅ Automatic daily backup is **ENABLED**")
            if last_backup:
                st.info(f"📅 Last backup: {last_backup}")
            else:
                st.info("📅 No backup performed yet")
        else:
            st.warning("⚠️ Automatic daily backup is **DISABLED**")
    
    with col2:
        # Toggle button
        if auto_backup_enabled:
            if st.button("🔴 Turn OFF Auto-Backup", width="stretch", type="secondary"):
                if set_backup_config("auto_backup_enabled", "No"):
                    st.success("Auto-backup disabled")
                    st.rerun()
                else:
                    st.error("Failed to update setting")
        else:
            if st.button("🟢 Turn ON Auto-Backup", width="stretch", type="primary"):
                if set_backup_config("auto_backup_enabled", "Yes"):
                    st.success("Auto-backup enabled")
                    st.rerun()
                else:
                    st.error("Failed to update setting")
    
    # Trigger manual backup now
    if auto_backup_enabled:
        if st.button("▶️ Run Backup Now", width="content"):
            with st.spinner("Creating backup..."):
                backup_file, error = perform_auto_backup()
                if error:
                    if "already performed today" in error:
                        st.info(error)
                    else:
                        st.error(error)
                else:
                    st.success(f"✅ Backup created successfully!")
                    st.caption(f"Saved to: {backup_file}")
    
    st.markdown(
        f"""
        <h2 style='text-align: center; color: #ffe066; font-size: 2.5em; font-family: Georgia, serif; letter-spacing: 2px; text-shadow: 1px 1px 8px #222;'>{get_active_company_name().upper()}</h2>
        """,
        unsafe_allow_html=True
    )
    st.markdown("""
    - Backups are stored in `data/backups/` folder
    - Old backups (older than 7 days) are automatically deleted
    - Manual backups can still be downloaded below
    """)
    
    st.divider()
    
    st.markdown("""
    ### Database Backup
    Download a complete backup of your database. This includes all tables, relationships, and data.
    """)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("📦 Full Database Backup")
        st.info("Download the SQLite database file")
        
        if st.button("📥 Download Database Backup", width="stretch", type="primary"):
            backup_data, error = create_database_backup()
            if error:
                st.error(error)
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                st.download_button(
                    "⬇️ Download business.db",
                    backup_data,
                    file_name=f"business_backup_{timestamp}.db",
                    mime="application/octet-stream",
                    width="stretch"
                )
                st.success("✅ Backup ready for download")
    
    with col2:
        st.subheader("📊 Export All Data (CSV)")
        st.info("Export all tables as CSV files in a ZIP archive")
        
        if st.button("📥 Download CSV Export", width="stretch"):
            zip_data, error = create_full_export_zip()
            if error:
                st.error(error)
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                st.download_button(
                    "⬇️ Download data_export.zip",
                    zip_data,
                    file_name=f"data_export_{timestamp}.zip",
                    mime="application/zip",
                    width="stretch"
                )
                st.success("✅ Export ready for download")
    
    st.divider()
    
    st.markdown("""
    ### 📋 Backup Instructions
    
    1. **Auto-Backup**: Automatically saves daily backups to `data/backups/` folder
    2. **Database Backup (.db)**: Complete SQLite database that can be restored by replacing the existing database file
    3. **CSV Export (.zip)**: Human-readable CSV files for each table - useful for data analysis in Excel
    
    **Recommended Schedule**: Enable auto-backup for daily protection, download manual backups for off-site storage
    """)
    
    st.divider()
    
    # Database Reset Section
    st.markdown("### 🔄 Reset Transactional Data")
    st.warning(
        """**CAUTION**: This will delete ALL transactional data including:

- Invoices, Bills, Estimates, Purchase Orders
- Expenses, Payments (Customer & Vendor)
- Loan Transactions

**The following will be preserved:**
- Customers (with open balances)
- Vendors (with open balances)
- Inventory (items, quantities, prices)
- Loan Parties (with balances)
- Customer & Vendor pricing matrices
        """
    )
    
    col1, col2 = st.columns([3, 1])
    with col1:
        st.info("Use this to start fresh while keeping your master data intact. **Create a backup first!**")
    with col2:
        if st.button("🗑️ Reset Data", type="secondary", width="stretch"):
            st.session_state.show_reset_confirm = True
    
    # Confirmation dialog
    if st.session_state.get('show_reset_confirm', False):
        st.error("### ⚠️ FINAL CONFIRMATION")
        st.write("Type **RESET** below to confirm deletion of all transactional data:")
        
        confirm_text = st.text_input("Confirmation", key="reset_confirm_input")
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Yes, Reset Now", type="primary", width="stretch"):
                if confirm_text == "RESET":
                    with st.spinner("Resetting database..."):
                        success, message = reset_transactional_data()
                        if success:
                            st.success(message)
                            st.balloons()
                            st.session_state.show_reset_confirm = False
                            time.sleep(2)
                            st.rerun()
                        else:
                            st.error(message)
                else:
                    st.error("❌ You must type RESET to confirm")
        
        with col2:
            if st.button("❌ Cancel", width="stretch"):
                st.session_state.show_reset_confirm = False
                st.rerun()

if __name__ == "__main__":
    bootstrap_company_registry()
    main()



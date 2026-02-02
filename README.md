# Abaseen ERP - Business Management System

A comprehensive Enterprise Resource Planning (ERP) system for managing inventory, sales, purchases, customers, vendors, and financial reporting.

## 🚀 Features

- 📊 **Dashboard** - Real-time financial metrics and KPIs
- 📝 **Sales Management** - Estimates, Invoices, Customer tracking
- 💰 **Purchase Management** - Bills, Purchase Orders, Vendor tracking
- 📦 **Inventory Management** - Stock tracking, Low stock alerts, CSV import, Pricing matrices
- 👥 **Customer Management** - Customer database with custom pricing
- 🏢 **Vendor Management** - Vendor database and ledgers
- 💵 **Payments** - Receive payments, Pay vendors, Loan management
- 💸 **Expenses** - Multi-category expense tracking (Salary, Utilities, Rent, Fuel, Food, etc.)
- 📈 **Reports** - Sales & Profit, P&L Statement, Customer/Vendor Ledgers with PDF export
- 💾 **Backup & Export** - Auto-backup, Database backup, CSV export, Data reset

## 🔐 Default Login

**Username:** `admin`  
**Password:** `Admin_786`

⚠️ **Important:** Change the default password immediately after first login!

## 🌐 Deployment

This app is configured for deployment on:
- **Fly.io** (Recommended - Free tier with persistent storage)
- **Streamlit Cloud** (Easy deployment from GitHub)
- **Railway** (Alternative with persistent storage)
- **Render** (Alternative platform)

### Deploy Files Included:
- `Dockerfile` - Docker container configuration
- `fly.toml` - Fly.io deployment settings
- `Procfile` - Alternative platform deployment
- `.streamlit/config.toml` - Streamlit configuration

## 📦 Technology Stack

- **Backend:** Python, Streamlit
- **Database:** SQLite
- **PDF Generation:** FPDF, ReportLab
- **Data Processing:** Pandas
- **Authentication:** PBKDF2-HMAC SHA256

## 🔒 Security Features

- Password hashing with 200,000 iterations
- Session-based authentication
- Database file excluded from version control
- HTTPS support (automatic on deployment platforms)

## 📊 Database Tables

- Inventory, Customers, Vendors
- Invoices, Bills, Estimates, Purchase Orders
- Expenses, Payments (Customer & Vendor)
- Loan Parties, Loan Transactions
- Customer/Vendor Pricing Matrices

## 📄 License

Proprietary - All rights reserved

---

**Developed by Abdul Hanan**  
Contact: 03125817185

### Notes
- Keep the `data` folder safe - it contains all your business data
- Regular backups are recommended (use Backup feature in the app)
- Do not delete or modify files unless you know what you're doing

---
**Abaseen General Order Suppliers** © 2026

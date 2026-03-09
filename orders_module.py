from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import pandas as pd
import streamlit as st


def init_orders_tables(conn) -> None:
    """Create orders tables (safe to call multiple times)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            OrderID TEXT PRIMARY KEY,
            Date TEXT NOT NULL,
            CustomerID TEXT,
            CustomerName TEXT NOT NULL,
            Status TEXT NOT NULL DEFAULT 'Open',
            CreatedAt TEXT NOT NULL,
            FilledAt TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_items (
            LineID TEXT PRIMARY KEY,
            OrderID TEXT NOT NULL,
            ItemID TEXT,
            ItemName TEXT NOT NULL,
            Quantity INTEGER NOT NULL CHECK(Quantity > 0),
            FOREIGN KEY(OrderID) REFERENCES orders(OrderID) ON DELETE CASCADE
        )
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(Status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(Date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(OrderID)")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _generate_order_id(conn) -> str:
    """Generate an order number like ORD-YYYYMMDD-001."""
    day = datetime.now().strftime("%Y%m%d")
    prefix = f"ORD-{day}-"

    row = conn.execute(
        "SELECT OrderID FROM orders WHERE OrderID LIKE ? ORDER BY OrderID DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()

    if not row:
        return f"{prefix}001"

    last_id = str(row[0])
    try:
        last_seq = int(last_id.split("-")[-1])
    except Exception:
        last_seq = 0

    return f"{prefix}{str(last_seq + 1).zfill(3)}"


def _read_sql_df(conn, sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, conn, params=params).fillna("")
    except Exception:
        return pd.DataFrame()


@dataclass
class OrderItemDraft:
    item_id: str
    item_name: str
    qty: int


def show_orders_page(*, get_db: Callable, load_table: Callable) -> None:
    """Standalone Orders module page.

    Args:
        get_db: contextmanager yielding sqlite3 connection (same pattern as app.py).
        load_table: function to load existing tables (customers/inventory).
    """

    st.title("📋 Orders")

    # Ensure tables exist for current company DB
    with get_db() as conn:
        init_orders_tables(conn)

    pending_tab, completed_tab = st.tabs(["🕒 Pending Orders", "✅ Completed Orders"])

    with pending_tab:
        _pending_orders_ui(get_db=get_db, load_table=load_table)

    with completed_tab:
        _completed_orders_ui(get_db=get_db)


def _pending_orders_ui(*, get_db: Callable, load_table: Callable) -> None:
    st.subheader("Create a new order")

    if "order_draft_items" not in st.session_state:
        st.session_state.order_draft_items = []

    customers = load_table("customers")
    inventory = load_table("inventory")

    col_left, col_right = st.columns([2, 1])
    with col_left:
        order_date = st.date_input("Order Date", value=datetime.now())
    with col_right:
        if st.button("🧹 Clear Draft", type="secondary", use_container_width=True):
            st.session_state.order_draft_items = []
            st.rerun()

    customer_id: Optional[str] = None
    customer_name: str = ""

    if customers is not None and not customers.empty:
        customer_names = customers["CustomerName"].astype(str).tolist()
        selection = st.selectbox("Customer", options=["— Select —"] + customer_names + ["Other / Walk-in"], index=0)
        if selection and selection not in ["— Select —", "Other / Walk-in"]:
            customer_name = selection
            match = customers[customers["CustomerName"].astype(str) == selection]
            if not match.empty and "CustomerID" in match.columns:
                customer_id = str(match.iloc[0]["CustomerID"])
        elif selection == "Other / Walk-in":
            customer_name = st.text_input("Customer Name")
    else:
        customer_name = st.text_input("Customer Name")

    st.markdown("---")
    st.subheader("Add items")

    add_cols = st.columns([3, 1, 1])

    item_id: str = ""
    item_name: str = ""

    if inventory is not None and not inventory.empty:
        with add_cols[0]:
            item_name = st.selectbox("Item", options=inventory["ItemName"].astype(str).tolist(), index=0)
        match = inventory[inventory["ItemName"].astype(str) == item_name]
        if not match.empty and "ItemID" in match.columns:
            item_id = str(match.iloc[0]["ItemID"])
    else:
        with add_cols[0]:
            item_name = st.text_input("Item Name")

    with add_cols[1]:
        qty = st.number_input("Qty", min_value=1, step=1, value=1)

    with add_cols[2]:
        if st.button("➕ Add", type="primary", use_container_width=True):
            if not item_name:
                st.error("Item is required")
            else:
                st.session_state.order_draft_items.append(
                    {
                        "item_id": item_id,
                        "item_name": item_name,
                        "qty": int(qty),
                    }
                )
                st.rerun()

    if st.session_state.order_draft_items:
        draft_df = pd.DataFrame(st.session_state.order_draft_items)
        draft_df = draft_df[["item_name", "qty"]].rename(columns={"item_name": "Item", "qty": "Qty"})
        st.dataframe(draft_df, use_container_width=True, hide_index=True)
    else:
        st.info("No items added yet.")

    create_cols = st.columns([2, 1])
    with create_cols[1]:
        if st.button("🧾 Create Order", use_container_width=True):
            if not customer_name.strip():
                st.error("Customer name is required")
            elif not st.session_state.order_draft_items:
                st.error("Please add at least one item")
            else:
                _create_order(
                    get_db=get_db,
                    order_date=order_date.strftime("%Y-%m-%d"),
                    customer_id=(customer_id or ""),
                    customer_name=customer_name.strip(),
                    items=st.session_state.order_draft_items,
                )
                st.session_state.order_draft_items = []
                st.success("✅ Order created")
                st.rerun()

    st.markdown("---")
    st.subheader("Pending orders")
    _orders_list_ui(get_db=get_db, status="Open", load_table=load_table)


def _completed_orders_ui(*, get_db: Callable) -> None:
    st.subheader("Completed orders")
    _orders_list_ui(get_db=get_db, status="Filled")


def _create_order(*, get_db: Callable, order_date: str, customer_id: str, customer_name: str, items: list[dict]) -> str:
    with get_db() as conn:
        init_orders_tables(conn)
        order_id = _generate_order_id(conn)
        created_at = _now_str()

        conn.execute(
            "INSERT INTO orders (OrderID, Date, CustomerID, CustomerName, Status, CreatedAt, FilledAt) VALUES (?, ?, ?, ?, 'Open', ?, NULL)",
            (order_id, order_date, (customer_id or None), customer_name, created_at),
        )

        for i, it in enumerate(items, start=1):
            line_id = f"{order_id}-L{str(i).zfill(3)}"
            conn.execute(
                "INSERT INTO order_items (LineID, OrderID, ItemID, ItemName, Quantity) VALUES (?, ?, ?, ?, ?)",
                (
                    line_id,
                    order_id,
                    (it.get("item_id") or None),
                    str(it.get("item_name") or ""),
                    int(it.get("qty") or 0),
                ),
            )

    return order_id


def _mark_order_filled(*, get_db: Callable, order_id: str) -> None:
    with get_db() as conn:
        init_orders_tables(conn)
        conn.execute(
            "UPDATE orders SET Status = 'Filled', FilledAt = ? WHERE OrderID = ?",
            (_now_str(), order_id),
        )


def _delete_order(*, get_db: Callable, order_id: str) -> None:
    with get_db() as conn:
        init_orders_tables(conn)
        # Delete items first for compatibility with older SQLite setups
        conn.execute("DELETE FROM order_items WHERE OrderID = ?", (order_id,))
        conn.execute("DELETE FROM orders WHERE OrderID = ? AND Status = 'Open'", (order_id,))


def _update_order_header(*, get_db: Callable, order_id: str, order_date: str, customer_name: str) -> None:
    with get_db() as conn:
        init_orders_tables(conn)
        conn.execute(
            "UPDATE orders SET Date = ?, CustomerName = ? WHERE OrderID = ? AND Status = 'Open'",
            (order_date, customer_name, order_id),
        )


def _update_order_item_qty(*, get_db: Callable, line_id: str, qty: int) -> None:
    with get_db() as conn:
        init_orders_tables(conn)
        conn.execute(
            "UPDATE order_items SET Quantity = ? WHERE LineID = ?",
            (int(qty), line_id),
        )


def _delete_order_line(*, get_db: Callable, line_id: str) -> None:
    with get_db() as conn:
        init_orders_tables(conn)
        conn.execute("DELETE FROM order_items WHERE LineID = ?", (line_id,))


def _next_line_id(conn, order_id: str) -> str:
    row = conn.execute(
        "SELECT LineID FROM order_items WHERE OrderID = ? ORDER BY LineID DESC LIMIT 1",
        (order_id,),
    ).fetchone()
    if not row:
        return f"{order_id}-L001"
    last_line_id = str(row[0])
    try:
        last_seq = int(last_line_id.split("-L")[-1])
    except Exception:
        last_seq = 0
    return f"{order_id}-L{str(last_seq + 1).zfill(3)}"


def _add_order_line(*, get_db: Callable, order_id: str, item_id: str, item_name: str, qty: int) -> None:
    with get_db() as conn:
        init_orders_tables(conn)
        line_id = _next_line_id(conn, order_id)
        conn.execute(
            "INSERT INTO order_items (LineID, OrderID, ItemID, ItemName, Quantity) VALUES (?, ?, ?, ?, ?)",
            (line_id, order_id, (item_id or None), item_name, int(qty)),
        )


def _orders_list_ui(*, get_db: Callable, status: str, load_table: Optional[Callable] = None) -> None:
    with get_db() as conn:
        init_orders_tables(conn)
        orders_df = _read_sql_df(
            conn,
            "SELECT OrderID, Date, CustomerName, Status, CreatedAt, FilledAt FROM orders WHERE Status = ? ORDER BY Date DESC, CreatedAt DESC",
            (status,),
        )

        if orders_df.empty:
            st.info("No orders found.")
            return

        for _, o in orders_df.iterrows():
            order_id = str(o.get("OrderID", ""))
            date = str(o.get("Date", ""))
            customer = str(o.get("CustomerName", ""))

            header = f"{order_id} • {date} • {customer}"
            with st.expander(header, expanded=False):
                items_df = _read_sql_df(
                    conn,
                    "SELECT LineID, ItemID, ItemName, Quantity FROM order_items WHERE OrderID = ? ORDER BY LineID",
                    (order_id,),
                )
                if not items_df.empty:
                    display_df = items_df[["ItemName", "Quantity"]].rename(columns={"ItemName": "Item", "Quantity": "Qty"})
                    st.dataframe(display_df, use_container_width=True, hide_index=True)
                else:
                    st.write("No items")

                if status == "Open":
                    btn_cols = st.columns(3)
                    with btn_cols[0]:
                        if st.button("✅ Order Filled", key=f"fill_{order_id}", use_container_width=True):
                            _mark_order_filled(get_db=get_db, order_id=order_id)
                            st.success("Marked as completed")
                            st.rerun()

                    edit_key = f"edit_{order_id}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False

                    with btn_cols[1]:
                        if not st.session_state[edit_key]:
                            if st.button("✏️ Edit", key=f"edit_btn_{order_id}", use_container_width=True, type="secondary"):
                                st.session_state[edit_key] = True
                                st.rerun()
                        else:
                            if st.button("↩️ Cancel", key=f"edit_cancel_{order_id}", use_container_width=True, type="secondary"):
                                st.session_state[edit_key] = False
                                st.rerun()

                    confirm_key = f"confirm_delete_{order_id}"
                    if confirm_key not in st.session_state:
                        st.session_state[confirm_key] = False

                    with btn_cols[2]:
                        if not st.session_state[confirm_key]:
                            if st.button("🗑️ Delete", key=f"del_{order_id}", use_container_width=True, type="secondary"):
                                st.session_state[confirm_key] = True
                                st.rerun()
                        else:
                            if st.button("⚠️ Confirm Delete", key=f"del_confirm_{order_id}", use_container_width=True, type="primary"):
                                _delete_order(get_db=get_db, order_id=order_id)
                                st.session_state[confirm_key] = False
                                st.success("Deleted")
                                st.rerun()

                    if st.session_state[edit_key]:
                        st.markdown("---")
                        st.subheader("Edit order")

                        try:
                            order_date_default = datetime.strptime(date, "%Y-%m-%d")
                        except Exception:
                            order_date_default = datetime.now()

                        with st.form(f"edit_form_{order_id}"):
                            c1, c2 = st.columns(2)
                            with c1:
                                new_date = st.date_input("Order Date", value=order_date_default, key=f"edit_date_{order_id}")
                            with c2:
                                new_customer = st.text_input("Customer Name", value=customer, key=f"edit_cust_{order_id}")

                            st.markdown("**Items**")
                            deletes: list[str] = []
                            changes: list[tuple[str, int]] = []

                            if items_df.empty:
                                st.info("No items in this order.")
                            else:
                                for _, it in items_df.iterrows():
                                    line_id = str(it.get("LineID", ""))
                                    item_name = str(it.get("ItemName", ""))
                                    current_qty = int(it.get("Quantity", 1) or 1)

                                    row_cols = st.columns([3, 1, 1])
                                    row_cols[0].write(item_name)
                                    qty_val = row_cols[1].number_input(
                                        "Qty",
                                        min_value=1,
                                        step=1,
                                        value=max(1, current_qty),
                                        key=f"edit_qty_{line_id}",
                                        label_visibility="collapsed",
                                    )
                                    remove = row_cols[2].checkbox(
                                        "Remove",
                                        value=False,
                                        key=f"edit_remove_{line_id}",
                                        label_visibility="collapsed",
                                    )

                                    changes.append((line_id, int(qty_val)))
                                    if remove:
                                        deletes.append(line_id)

                            saved = st.form_submit_button("💾 Save Changes", type="primary")
                            if saved:
                                if not str(new_customer).strip():
                                    st.error("Customer name is required")
                                else:
                                    _update_order_header(
                                        get_db=get_db,
                                        order_id=order_id,
                                        order_date=new_date.strftime("%Y-%m-%d"),
                                        customer_name=str(new_customer).strip(),
                                    )

                                    delete_set = set(deletes)
                                    for lid in deletes:
                                        _delete_order_line(get_db=get_db, line_id=lid)
                                    for lid, q in changes:
                                        if lid in delete_set:
                                            continue
                                        _update_order_item_qty(get_db=get_db, line_id=lid, qty=int(q))

                                    st.session_state[edit_key] = False
                                    st.success("Saved")
                                    st.rerun()

                        st.markdown("**Add item**")
                        inventory = pd.DataFrame()
                        if load_table is not None:
                            try:
                                inventory = load_table("inventory")
                            except Exception:
                                inventory = pd.DataFrame()

                        with st.form(f"add_line_{order_id}"):
                            new_item_id = ""
                            new_item_name = ""
                            if inventory is not None and not inventory.empty:
                                new_item_name = st.selectbox(
                                    "Item",
                                    options=inventory["ItemName"].astype(str).tolist(),
                                    key=f"add_item_{order_id}",
                                )
                                match = inventory[inventory["ItemName"].astype(str) == new_item_name]
                                if not match.empty and "ItemID" in match.columns:
                                    new_item_id = str(match.iloc[0]["ItemID"])
                            else:
                                new_item_name = st.text_input("Item Name", key=f"add_item_text_{order_id}")

                            new_qty = st.number_input("Qty", min_value=1, step=1, value=1, key=f"add_qty_{order_id}")
                            added = st.form_submit_button("➕ Add Item")
                            if added:
                                if not str(new_item_name).strip():
                                    st.error("Item is required")
                                else:
                                    _add_order_line(
                                        get_db=get_db,
                                        order_id=order_id,
                                        item_id=new_item_id,
                                        item_name=str(new_item_name).strip(),
                                        qty=int(new_qty),
                                    )
                                    st.success("Item added")
                                    st.rerun()
                else:
                    filled_at = str(o.get("FilledAt", ""))
                    if filled_at:
                        st.caption(f"Filled at: {filled_at}")

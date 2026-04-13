# maintenance_store_full.py
# Single-file Streamlit app with full user management, material requests, operator workflow and inventory audit.
# Run: pip install streamlit pandas sqlalchemy openpyxl
# Then: streamlit run maintenance_store_full.py

import streamlit as st
import pandas as pd
import hashlib
import os
from datetime import datetime, date
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, UniqueConstraint, Boolean, Enum, ForeignKey
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import io

# ---------------- Database ----------------

DB_FILE = 'maintenance_store_full.db'
DB_URL = f'sqlite:///{DB_FILE}'
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
Base = declarative_base()

def now():
    return datetime.utcnow()

# Users table
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)  # store salt+hash hex
    role = Column(String, nullable=False)  # Admin, Manager, Operator, Requester
    name = Column(String, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)

# Items table
class Item(Base):
    __tablename__ = 'items'
    id = Column(Integer, primary_key=True)
    item_code = Column(String, nullable=False)
    item_name = Column(String, nullable=False)
    location = Column(String, nullable=True)
    contact = Column(String, nullable=True)
    quantity = Column(Integer, default=0)
    unit = Column(String, nullable=True)
    __table_args__ = (UniqueConstraint('item_code', name='uix_item_code'),)

# Transactions (add/issue)
class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True)
    item_code = Column(String, nullable=False)
    item_name = Column(String, nullable=False)
    change = Column(Integer, nullable=False)  # positive add, negative issue
    note = Column(Text, nullable=True)
    user = Column(String, nullable=True)  # who performed
    timestamp = Column(DateTime, default=now)

# Material requests
class MaterialRequest(Base):
    __tablename__ = 'material_requests'
    id = Column(Integer, primary_key=True)
    item_code = Column(String, nullable=False)
    quantity_required = Column(Integer, nullable=False)
    work_location = Column(String, nullable=True)
    requested_by = Column(String, nullable=False)  # username
    priority = Column(String, default='Normal')
    note = Column(Text, nullable=True)
    status = Column(String, default='Pending')  # Pending / Completed / Rejected
    timestamp = Column(DateTime, default=now)
    completed_at = Column(DateTime, nullable=True)
    completed_by = Column(String, nullable=True)

# Inventory audit logs
class InventoryAudit(Base):
    __tablename__ = 'inventory_audit'
    id = Column(Integer, primary_key=True)
    item_code = Column(String, nullable=False)
    system_quantity = Column(Integer, nullable=False)
    physical_quantity = Column(Integer, nullable=False)
    difference = Column(Integer, nullable=False)
    done_by = Column(String, nullable=False)
    timestamp = Column(DateTime, default=now)

Base.metadata.create_all(engine)
# --- Create default admin if not exists ---
def create_default_admin():
    sess = Session()
    existing = sess.query(User).filter_by(username="admin").first()
    if not existing:
        salt = generate_salt()
        ph = hash_password("admin123", salt)
        admin = User(
            username="admin",
            password_hash=ph,
            role="Admin",
            name="Administrator",
            active=True
        )
        sess.add(admin)
        sess.commit()
    sess.close()

create_default_admin()

# ---------------- Utilities: password hashing & user helpers ----------------

def generate_salt():
    return os.urandom(8).hex()

def hash_password(password: str, salt: str):
    # simple sha256(salt + password)
    to_hash = (salt + password).encode('utf-8')
    h = hashlib.sha256(to_hash).hexdigest()
    return f"{salt}${h}"

def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt, h = stored_hash.split('$')
    except Exception:
        return False
    return hash_password(password, salt) == stored_hash

def create_user(username, password, role, name=None):
    # basic validation
    if not username or not password or not role:
        return False, "Username, password and role are required"
    sess = Session()
    try:
        if sess.query(User).filter_by(username=username).first():
            return False, "Username already exists"
        salt = generate_salt()
        ph = hash_password(password, salt)
        user = User(username=username, password_hash=ph, role=role, name=name or username, active=True)
        sess.add(user)
        sess.commit()
        return True, "User created"
    except Exception as e:
        sess.rollback()
        return False, f"DB error: {e}"
    finally:
        sess.close()


# ---------------- Inventory helpers ----------------

def get_session():
    return Session()

def import_excel_items(df: pd.DataFrame):
    sess = get_session()
    added = 0
    updated = 0
    for _, row in df.iterrows():
        code = str(row.get('item_code', '')).strip()
        if not code:
            continue
        name = str(row.get('item_name', '')).strip()
        loc = str(row.get('location', '')).strip()
        try:
            qty = int(row.get('quantity', 0))
        except:
            qty = 0
        unit = str(row.get('unit', '')).strip()
        existing = sess.query(Item).filter_by(item_code=code).first()
        if existing:
            existing.item_name = name or existing.item_name
            existing.location = loc or existing.location
            existing.unit = unit or existing.unit
            existing.quantity = qty
            updated += 1
        else:
            it = Item(item_code=code, item_name=name, location=loc, quantity=qty, unit=unit)
            sess.add(it)
            added += 1
    sess.commit()
    sess.close()
    return added, updated

def add_item_db(code, name, loc, contact, qty, unit):
    sess = get_session()
    if sess.query(Item).filter_by(item_code=code).first():
        sess.close()
        return False, "exists"
    it = Item(item_code=code, item_name=name, location=loc, contact=contact, quantity=int(qty), unit=unit)
    sess.add(it)
    sess.commit()
    sess.close()
    return True, "added"

def add_stock_db(code, qty, user, note=''):
    sess = get_session()
    it = sess.query(Item).filter_by(item_code=code).first()
    if not it:
        sess.close()
        return False, "not_found"
    it.quantity += int(qty)
    txn = Transaction(item_code=code, item_name=it.item_name, change=int(qty), note=note, user=user)
    sess.add(txn)
    sess.commit()
    qty_left = it.quantity
    sess.close()
    return True, qty_left

def issue_item_db(code, qty, user, work_location='', note=''):
    sess = get_session()
    it = sess.query(Item).filter_by(item_code=code).first()
    if not it:
        sess.close()
        return False, "not_found", 0
    if it.quantity < int(qty):
        available = it.quantity
        sess.close()
        return False, "insufficient", available
    it.quantity -= int(qty)
    txn = Transaction(item_code=code, item_name=it.item_name, change=-int(qty), note=note, user=user)
    sess.add(txn)
    sess.commit()
    remaining = it.quantity
    sess.close()
    return True, "ok", remaining

def get_inventory_df():
    sess = get_session()
    items = sess.query(Item).order_by(Item.item_code).all()
    sess.close()
    rows = []
    for it in items:
        rows.append({
            'item_code': it.item_code,
            'item_name': it.item_name,
            'location': it.location,
            'contact': it.contact,
            'quantity': it.quantity,
            'unit': it.unit
        })
    return pd.DataFrame(rows)

def get_zero_stock_df():
    df = get_inventory_df()
    if df.empty:
        return df
    return df[df['quantity'] <= 0]

def get_transactions_df():
    sess = get_session()
    txs = sess.query(Transaction).order_by(Transaction.timestamp.desc()).all()
    sess.close()
    rows = []
    for t in txs:
        rows.append({
            'timestamp': t.timestamp,
            'item_code': t.item_code,
            'item_name': t.item_name,
            'change': t.change,
            'note': t.note,
            'user': t.user
        })
    return pd.DataFrame(rows)

def get_requests_df():
    sess = get_session()
    reqs = sess.query(MaterialRequest).order_by(MaterialRequest.timestamp.desc()).all()
    sess.close()
    rows = []
    for r in reqs:
        rows.append({
            'id': r.id,
            'item_code': r.item_code,
            'quantity_required': r.quantity_required,
            'work_location': r.work_location,
            'requested_by': r.requested_by,
            'priority': r.priority,
            'note': r.note,
            'status': r.status,
            'timestamp': r.timestamp,
            'completed_at': r.completed_at,
            'completed_by': r.completed_by
        })
    return pd.DataFrame(rows)

def get_audit_df():
    sess = get_session()
    audits = sess.query(InventoryAudit).order_by(InventoryAudit.timestamp.desc()).all()
    sess.close()
    rows = []
    for a in audits:
        rows.append({
            'id': a.id,
            'item_code': a.item_code,
            'system_quantity': a.system_quantity,
            'physical_quantity': a.physical_quantity,
            'difference': a.difference,
            'done_by': a.done_by,
            'timestamp': a.timestamp
        })
    return pd.DataFrame(rows)

def get_users_df():
    sess = get_session()
    users = sess.query(User).order_by(User.username).all()
    sess.close()
    rows = []
    for u in users:
        rows.append({
            'username': u.username,
            'role': u.role,
            'name': u.name,
            'active': u.active,
            'created_at': u.created_at
        })
    return pd.DataFrame(rows)

# ---------- SEARCH HELPER ----------
def search_items_df(keyword=None):
    df = get_inventory_df()
    if df.empty:
        return df

    if keyword and keyword.strip():
        k = keyword.lower().strip()
        df = df[
            df['item_code'].astype(str).str.lower().str.contains(k) |
            df['item_name'].astype(str).str.lower().str.contains(k)
        ]
    return df

# ---------------- Streamlit UI ----------------

st.set_page_config(page_title="Maintenance Store (Full)", layout="wide")
st.title("Maintenance Store — Full System")

# --- Login (improved with session_state) ---
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.role = None
    st.session_state.current_user = None

# If already logged in, show logout button and info
if st.session_state.logged_in:
    st.sidebar.success(f"Logged in as: {st.session_state.current_user} ({st.session_state.role})")
    if st.sidebar.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.role = None
        st.session_state.current_user = None
        st.rerun()

else:
    username = st.sidebar.text_input("Username", key="login_username")
    password = st.sidebar.text_input("Password", type="password", key="login_password")
    login_btn = st.sidebar.button("Login", key="login_btn")

    if login_btn:
        sess = get_session()
        user = sess.query(User).filter_by(username=username, active=True).first()
        sess.close()
        
        if not user or not verify_password(user.password_hash, password):
            st.sidebar.error("Invalid credentials or inactive user")
        else:
            # success -> store in session_state so future reruns keep us logged in
            st.session_state.logged_in = True
            st.session_state.role = user.role
            st.session_state.current_user = user.username
            
            st.sidebar.success("Login successful")
            st.rerun()   # ✔️ Replaced experimental_rerun

# stop if not logged in
if not st.session_state.logged_in:
    st.sidebar.info("Enter credentials and press Login. Default admin: admin / admin123")
    st.stop()

# set ROLE and CURRENT_USER from session_state for rest of code
ROLE = st.session_state.role
CURRENT_USER = st.session_state.current_user


# --- ADMIN DASHBOARD ---
if ROLE == 'Admin':
    st.header("Admin Dashboard")
    st.subheader("User Management")
    with st.expander("Create new user"):
        new_username = st.text_input("New username", key="nu")
        new_pass = st.text_input("New password", key="np")
        new_role = st.selectbox("Role", ['Admin', 'Manager', 'Operator', 'Requester'], key="nr")
        new_name = st.text_input("Full name (optional)", key="nname")
        create_btn = st.button("Create user", key="create_user")
        if create_btn:
            ok, msg = create_user(new_username, new_pass, new_role, new_name)
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    with st.expander("Existing users"):
        df_users = get_users_df()
        st.dataframe(df_users)
        # simple actions: disable/enable/reset password
        st.markdown("**Reset password / Disable user**")
        sel_user = st.text_input("Username to modify", key="mod_user")
        new_pw = st.text_input("New password (leave empty to keep)", key="mod_pw")
        active_choice = st.selectbox("Active?", ['yes', 'no'], key="mod_active")
        apply_mod = st.button("Apply changes", key="apply_mod")
        if apply_mod:
            sess = get_session()
            u = sess.query(User).filter_by(username=sel_user).first()
            if not u:
                st.error("User not found")
            else:
                if new_pw:
                    salt = generate_salt()
                    u.password_hash = hash_password(new_pw, salt)
                u.active = True if active_choice == 'yes' else False
                sess.commit()
                sess.close()
                st.success("Updated")

    st.subheader("Items (Bulk import / Add single)")
    with st.expander("Bulk import items from Excel"):
        file = st.file_uploader("Upload .xlsx or .csv", key="bulk_items")
        if file:
            try:
                if str(file.name).lower().endswith('.csv'):
                    df = pd.read_csv(file)
                else:
                    df = pd.read_excel(file)
                added, updated = import_excel_items(df)
                st.success(f"Added: {added}, Updated: {updated}")
            except Exception as e:
                st.error("Import failed: " + str(e))

    with st.form("add_item_form"):
        st.markdown("Add single item")
        ic = st.text_input("Item Code")
        iname = st.text_input("Item Name")
        iloc = st.text_input("Location")
        icont = st.text_input("Contact")
        iqty = st.number_input("Quantity", min_value=0, value=0)
        iunit = st.text_input("Unit")
        sub = st.form_submit_button("Add item")
        if sub:
            ok, msg = add_item_db(ic, iname, iloc, icont, iqty, iunit)
            if ok:
                st.success("Item added")
            else:
                st.error(msg)

    st.subheader("Inventory")
    st.dataframe(get_inventory_df())

    st.subheader("Zero / Low stock")
    st.dataframe(get_zero_stock_df())

    st.subheader("Requests")
    st.dataframe(get_requests_df())

    st.subheader("Audits")
    st.dataframe(get_audit_df())

    st.subheader("Transactions")
    st.dataframe(get_transactions_df())

    st.subheader("Download master reports (Excel)")
    if st.button("Download all reports (Excel)"):
        inv = get_inventory_df()
        tx = get_transactions_df()
        req = get_requests_df()
        audit = get_audit_df()
        users = get_users_df()
        bio = io.BytesIO()
        with pd.ExcelWriter(bio, engine='openpyxl') as writer:
            inv.to_excel(writer, index=False, sheet_name='inventory')
            tx.to_excel(writer, index=False, sheet_name='transactions')
            req.to_excel(writer, index=False, sheet_name='requests')
            audit.to_excel(writer, index=False, sheet_name='audits')
            users.to_excel(writer, index=False, sheet_name='users')
        bio.seek(0)
        st.download_button("Click to download all", bio, file_name=f'master_reports_{date.today()}.xlsx')

# --- MANAGER DASHBOARD ---
elif ROLE == 'Manager':
    st.header("Manager Dashboard")
    st.subheader("Add Stock (Material Received)")
    with st.form("mgr_add_stock"):
        code_add = st.text_input('Item Code to add stock')
        qty_add = st.number_input('Quantity to add', min_value=1, value=1)
        contact_add = st.text_input('Supplier / Received by')
        note_add = st.text_area('Note (optional) - e.g. invoice no')
        mgr_sub = st.form_submit_button('Add Stock')
        if mgr_sub:
            ok, res = add_stock_db(code_add.strip(), int(qty_add), CURRENT_USER, note_add)
            if not ok:
                st.error('Item not found. Ask Admin to add new item first.')
            else:
                st.success(f'Stock added. New quantity: {res}')

    st.subheader("Requests (view)")
    st.dataframe(get_requests_df())

    st.subheader("Download Reports")
    if st.button("Download Excel (inventory/transactions/requests/audits)"):
        inv = get_inventory_df()
        tx = get_transactions_df()
        req = get_requests_df()
        audit = get_audit_df()
        bio = io.BytesIO()
        with pd.ExcelWriter(bio, engine='openpyxl') as writer:
            inv.to_excel(writer, index=False, sheet_name='inventory')
            tx.to_excel(writer, index=False, sheet_name='transactions')
            req.to_excel(writer, index=False, sheet_name='requests')
            audit.to_excel(writer, index=False, sheet_name='audits')
        bio.seek(0)
        st.download_button("Click to download", bio, file_name=f'manager_reports_{date.today()}.xlsx')

# --- OPERATOR DASHBOARD ---
elif ROLE == 'Operator':
    st.header("Operator Dashboard")

    st.subheader("🔍 Search Material")
    op_search = st.text_input("Search Item Code / Name")
    st.dataframe(search_items_df(op_search))

    st.subheader("⚠️ Zero Stock Items")
    st.dataframe(get_zero_stock_df())

    st.subheader("Pending Material Requests")
    df_req = get_requests_df()
    pending = df_req[df_req['status'] == 'Pending']
    st.dataframe(pending)

    st.markdown("**Issue material for a request**")
    with st.form("issue_from_request"):
        req_id = st.number_input("Request ID", min_value=1, step=1)
        issue_qty = st.number_input("Quantity to issue (0 = full)", min_value=0)
        note = st.text_area("Note")
        do_issue = st.form_submit_button("Issue & Complete")

        if do_issue:
            sess = get_session()
            r = sess.query(MaterialRequest).filter_by(id=int(req_id)).first()
            if not r:
                st.error("Request not found")
            else:
                qty_to_issue = issue_qty if issue_qty > 0 else r.quantity_required
                ok, reason, avail = issue_item_db(
                    r.item_code, qty_to_issue, CURRENT_USER, r.work_location, note
                )
                if not ok:
                    st.error(f"Error: {reason}, Available: {avail}")
                else:
                    r.status = 'Completed'
                    r.completed_at = now()
                    r.completed_by = CURRENT_USER
                    sess.commit()
                    sess.close()
                    st.success("Material issued & request completed")

    st.subheader("Transactions")
    st.dataframe(get_transactions_df())


# --- REQUESTER DASHBOARD ---
elif ROLE == 'Requester':
    st.header("Requester Dashboard")

    st.subheader("🔍 Search Material")
    req_search = st.text_input("Search Item Code / Name")
    st.dataframe(search_items_df(req_search))

    st.subheader("⚠️ Zero Stock Items")
    st.dataframe(get_zero_stock_df())

    with st.form("req_form"):
        item_code = st.text_input("Item Code")
        qty_req = st.number_input("Required Quantity", min_value=1)
        work_loc = st.text_input("Work Location")
        priority = st.selectbox("Priority", ["Normal", "High", "Low"])
        note = st.text_area("Note")
        send = st.form_submit_button("Submit Request")

        if send:
            sess = get_session()
            mr = MaterialRequest(
                item_code=item_code.strip(),
                quantity_required=qty_req,
                work_location=work_loc,
                requested_by=CURRENT_USER,
                priority=priority,
                note=note
            )
            sess.add(mr)
            sess.commit()
            sess.close()
            st.success("Request submitted successfully")

    st.subheader("My Requests")
    sess = get_session()
    myreqs = sess.query(MaterialRequest)\
        .filter_by(requested_by=CURRENT_USER)\
        .order_by(MaterialRequest.timestamp.desc()).all()
    sess.close()

    st.dataframe(pd.DataFrame([{
        "ID": r.id,
        "Item": r.item_code,
        "Qty": r.quantity_required,
        "Status": r.status,
        "Time": r.timestamp
    } for r in myreqs]))


# --- fallback ---
else:
    st.error("Your role is not configured. Contact Admin.")

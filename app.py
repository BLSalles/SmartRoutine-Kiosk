import os
import re
import json
import hmac
import base64
import hashlib
import sqlite3
import time
from datetime import datetime, date, timedelta
from decimal import Decimal

import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, current_app
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text

APP_TITLE = "SmartRoutine • Kiosk"
ABACATEPAY_API_BASE = "https://api.abacatepay.com"
ABACATEPAY_PUBLIC_HMAC_KEY = "t9dXRhHHo3yDEj5pVDYz0frf7q6bMKyMRmxxCPIPp3RCplBfXRxqlC6ZpiWmOqj4L63qEaeUOtrCI8P0VMUgo6iIga2ri9ogaHFs0WIIywSMg0q7RmBfybe1E5XJcfC4IW3alNqym0tXoAKkzvfEjZxV6bE0oG2zJrNNYmUCKZyV0KZ3JS8Votf9EAWWYdiDkMkpbMdPggfh1EqHlVkMiTady6jOR3hyzGEHrIz2Ret0xHKMbiqkr9HS1JhNHDX9"

db = SQLAlchemy()


def _normalize_cpf(cpf_raw: str) -> str:
    return re.sub(r"\D+", "", cpf_raw or "")


def _cpf_hash(cpf_digits: str, secret: str) -> str:
    data = (secret + ":" + cpf_digits).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _is_valid_cpf(cpf_digits: str) -> bool:
    """Valida CPF com dígitos verificadores (DV). Espera 11 dígitos."""
    if not cpf_digits or len(cpf_digits) != 11:
        return False
    # Rejeita sequências (000..., 111..., etc.)
    if cpf_digits == cpf_digits[0] * 11:
        return False

    def calc_digit(digs: str, factor: int) -> str:
        total = 0
        for ch in digs:
            total += int(ch) * factor
            factor -= 1
        r = (total * 10) % 11
        if r == 10:
            r = 0
        return str(r)

    d1 = calc_digit(cpf_digits[:9], 10)
    d2 = calc_digit(cpf_digits[:9] + d1, 11)
    return cpf_digits[-2:] == d1 + d2


def _abacate_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _abacate_enabled(app: Flask) -> bool:
    return bool(app.config.get("ABACATEPAY_API_KEY"))

def _safe_order_get(order_id):
    try:
        oid = int(order_id)
    except Exception:
        return None
    try:
        return db.session.get(Order, oid)
    except Exception:
        return Order.query.get(oid)


def _normalize_abacate_entity(data):
    """
    Normaliza payloads da AbacatePay para um formato único.
    Aceita respostas como {"billing": {...}, "payment": {...}} ou objetos diretos.
    """
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("billing"), dict):
        billing = dict(data.get("billing") or {})
        payment = data.get("payment") or {}
        if isinstance(payment, dict):
            billing["payment"] = payment
        if data.get("url") and not billing.get("url"):
            billing["url"] = data.get("url")
        if data.get("receiptUrl") and not billing.get("receiptUrl"):
            billing["receiptUrl"] = data.get("receiptUrl")
        if data.get("receipt_url") and not billing.get("receipt_url"):
            billing["receipt_url"] = data.get("receipt_url")
        return billing
    return data



def _verify_abacate_signature(raw_body: bytes, signature_from_header: str) -> bool:
    if not signature_from_header:
        return False
    expected_sig = base64.b64encode(
        hmac.new(ABACATEPAY_PUBLIC_HMAC_KEY.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected_sig, signature_from_header)


def _apply_paid_state(order, entity: dict | None = None):
    entity = _normalize_abacate_entity(entity or {})
    methods = entity.get("methods") or []
    payer = entity.get("payerInformation") or entity.get("payer_information") or {}
    payment = entity.get("payment") or {}
    method = (
        payer.get("method")
        or payment.get("method")
        or entity.get("method")
        or (methods[0] if methods else "ABACATEPAY")
    )
    order.payment_gateway = "ABACATEPAY"
    order.is_paid = True
    order.payment_status = "PAID"
    order.payment_method = (method or "ABACATEPAY").upper()
    if not order.paid_at:
        order.paid_at = datetime.utcnow()
    if order.status == "AGUARDANDO_PAGAMENTO":
        order.status = "RECEBIDO"
    order.payment_checkout_url = entity.get("url") or order.payment_checkout_url
    order.payment_receipt_url = entity.get("receiptUrl") or entity.get("receipt_url") or order.payment_receipt_url
    order.payment_external_id = entity.get("id") or order.payment_external_id




def _extract_order_id_from_external_id(value: str | None):
    ext = str(value or "").strip()
    if not ext.startswith("pedido-"):
        return None
    m = re.match(r"pedido-(\d+)(?:-|$)", ext)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _find_order_from_abacate_payload(entity: dict | None = None, data: dict | None = None, metadata: dict | None = None):
    entity = _normalize_abacate_entity(entity or {})
    data = data or {}
    metadata = metadata or {}

    candidates = [
        entity.get("externalId"),
        data.get("externalId"),
        metadata.get("externalId"),
        metadata.get("order_external_id"),
    ]
    for candidate in candidates:
        order_id = _extract_order_id_from_external_id(candidate)
        if order_id:
            order = _safe_order_get(order_id)
            if order:
                return order

    metadata_order_id = metadata.get("order_id") or metadata.get("orderId") or data.get("order_id") or data.get("orderId")
    if metadata_order_id:
        order = _safe_order_get(metadata_order_id)
        if order:
            return order

    entity_id = entity.get("id") or data.get("id")
    if entity_id:
        order = Order.query.filter_by(payment_external_id=entity_id).first()
        if order:
            return order

    products = entity.get("products") or data.get("products") or []
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, dict):
                continue
            order_id = _extract_order_id_from_external_id(product.get("externalId"))
            if order_id:
                order = _safe_order_get(order_id)
                if order:
                    return order

    payload_blob = json.dumps({"entity": entity, "data": data, "metadata": metadata}, ensure_ascii=False)
    matches = re.findall(r"pedido-(\d+)(?:-|\b)", payload_blob)
    for match in matches:
        order = _safe_order_get(match)
        if order:
            return order

    customer_md = ((entity.get("customer") or {}).get("metadata") or {})
    customer_email = (customer_md.get("email") or "").strip().lower()
    customer_name = (customer_md.get("name") or "").strip().lower()
    amount_cents = entity.get("amount") or data.get("amount")
    try:
        amount_value = (float(amount_cents) / 100.0) if amount_cents is not None else None
    except Exception:
        amount_value = None

    if amount_value is not None:
        q = (
            Order.query
            .filter(Order.status == "AGUARDANDO_PAGAMENTO")
            .filter(Order.payment_gateway == "ABACATEPAY")
            .order_by(Order.created_at.desc())
        )
        for candidate in q.limit(20).all():
            if abs(float(candidate.total or 0) - float(amount_value)) > 0.01:
                continue
            email_ok = (customer_email and (candidate.customer_email or "").strip().lower() == customer_email)
            name_ok = (customer_name and (candidate.customer_name or "").strip().lower() == customer_name)
            if customer_email and email_ok:
                return candidate
            if customer_name and name_ok:
                return candidate

    return None

def _apply_cancelled_state(order, status: str = "CANCELADO"):
    order.payment_gateway = "ABACATEPAY"
    order.payment_status = status or "CANCELADO"
    if not order.is_paid and order.status == "AGUARDANDO_PAGAMENTO":
        order.status = "CANCELADO"


def _fetch_abacate_entity(order, app: Flask):
    if not order or not _abacate_enabled(app):
        return False, None

    headers = _abacate_headers(app.config["ABACATEPAY_API_KEY"])
    lookup_params = []
    if order.payment_external_id:
        lookup_params.append((f"{ABACATEPAY_API_BASE}/v1/billing/list", {"id": order.payment_external_id}))
        lookup_params.append((f"{ABACATEPAY_API_BASE}/v2/checkouts/list", {"id": order.payment_external_id, "limit": 1}))
    lookup_params.append((f"{ABACATEPAY_API_BASE}/v1/billing/list", {"externalId": f"pedido-{order.id}"}))
    lookup_params.append((f"{ABACATEPAY_API_BASE}/v2/checkouts/list", {"externalId": f"pedido-{order.id}", "limit": 1}))

    for url, params in lookup_params:
        try:
            res = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception:
            continue
        if not res.ok:
            continue
        body = res.json() if res.content else {}
        data = (body or {}).get("data") or []
        if isinstance(data, dict):
            entity = _normalize_abacate_entity(data)
            if entity:
                return True, entity
        if isinstance(data, list) and data:
            entity = _normalize_abacate_entity(data[0])
            if entity:
                return True, entity
    return False, None

def _sync_order_payment_status(order, app: Flask):
    if not order or order.payment_gateway != "ABACATEPAY" or not _abacate_enabled(app):
        return False

    try:
        found, entity = _fetch_abacate_entity(order, app)
        if not found:
            return False

        status = ((entity or {}).get("status") or "").upper()
        order.payment_status = status or order.payment_status
        order.payment_checkout_url = (entity or {}).get("url") or order.payment_checkout_url
        order.payment_receipt_url = (entity or {}).get("receiptUrl") or (entity or {}).get("receipt_url") or order.payment_receipt_url
        order.payment_external_id = (entity or {}).get("id") or order.payment_external_id

        if status == "PAID":
            _apply_paid_state(order, entity)
        elif status in {"EXPIRED", "CANCELLED", "CANCELED"} and not order.is_paid:
            _apply_cancelled_state(order, "CANCELADO")

        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


def _reconcile_pending_abacate_orders(app: Flask, limit: int = 30):
    if not _abacate_enabled(app):
        return 0

    timeout_minutes = int(os.environ.get("ABACATEPAY_PENDING_TIMEOUT_MINUTES", "10") or "10")
    cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
    pending_orders = (
        Order.query
        .filter_by(payment_gateway="ABACATEPAY")
        .filter(Order.status == "AGUARDANDO_PAGAMENTO")
        .order_by(Order.created_at.asc())
        .limit(limit)
        .all()
    )
    updated = 0
    for order in pending_orders:
        try:
            found, entity = _fetch_abacate_entity(order, app)
            if found and entity:
                status = (entity.get("status") or "").upper()
                order.payment_status = status or order.payment_status
                order.payment_checkout_url = entity.get("url") or order.payment_checkout_url
                order.payment_receipt_url = entity.get("receiptUrl") or entity.get("receipt_url") or order.payment_receipt_url
                order.payment_external_id = entity.get("id") or order.payment_external_id
                if status == "PAID":
                    _apply_paid_state(order, entity)
                    updated += 1
                    continue
                if status in {"EXPIRED", "CANCELLED", "CANCELED"}:
                    _apply_cancelled_state(order, "CANCELADO")
                    updated += 1
                    continue
                if order.created_at <= cutoff and status in {"", "PENDING"}:
                    _apply_cancelled_state(order, "CANCELADO")
                    updated += 1
                    continue
            elif order.created_at <= cutoff and not order.is_paid:
                # Fail-safe local expiration when the pending window has passed.
                _apply_cancelled_state(order, "CANCELADO")
                updated += 1
        except Exception:
            db.session.rollback()
    if updated:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return 0
    return updated


def _create_abacate_checkout(order, customer_email: str, customer_cellphone: str, cpf_digits: str, app: Flask, base_url: str):
    products = []
    for item in OrderItem.query.filter_by(order_id=order.id).all():
        products.append({
            "externalId": f"pedido-{order.id}-item-{item.product_id}",
            "name": item.product.name,
            "description": f"Pedido #{order.id} - {item.product.name}",
            "quantity": int(item.qty),
            "price": int(round(float(item.unit_price) * 100)),
        })

    payload = {
        "frequency": "ONE_TIME",
        "methods": ["PIX", "CARD"],
        "products": products,
        "returnUrl": f"{base_url.rstrip('/')}" + url_for("cliente_pagamento", order_id=order.id),
        "completionUrl": f"{base_url.rstrip('/')}" + url_for("cliente_pagamento", order_id=order.id),
        "customer": {
            "name": order.customer_name,
            "email": customer_email,
            "cellphone": customer_cellphone,
            "taxId": cpf_digits,
        },
        "externalId": f"pedido-{order.id}",
        "metadata": {
            "order_id": str(order.id),
            "source": "smartroutine-kiosk",
        },
    }

    res = requests.post(
        f"{ABACATEPAY_API_BASE}/v1/billing/create",
        headers=_abacate_headers(app.config["ABACATEPAY_API_KEY"]),
        data=json.dumps(payload),
        timeout=25,
    )
    body = res.json() if res.content else {}
    if not res.ok or body.get("error"):
        raise RuntimeError(body.get("error") or f"Erro HTTP {res.status_code} ao criar checkout")

    checkout = (body or {}).get("data") or {}
    order.payment_gateway = "ABACATEPAY"
    order.payment_external_id = checkout.get("id")
    order.payment_checkout_url = checkout.get("url")
    order.payment_status = (checkout.get("status") or "PENDING").upper()
    order.payment_method = "ABACATEPAY"
    db.session.commit()
    return checkout


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Show demo credentials only if explicitly enabled
    app.config["SHOW_DEMO_CREDENTIALS"] = os.environ.get("SHOW_DEMO_CREDENTIALS", "false").lower() in {"1", "true", "yes", "y"}
    app.config["ABACATEPAY_API_KEY"] = os.environ.get("ABACATEPAY_API_KEY", "").strip()
    app.config["ABACATEPAY_WEBHOOK_SECRET"] = os.environ.get("ABACATEPAY_WEBHOOK_SECRET", "").strip()

    # Put relative sqlite db inside instance/
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///") and "://" not in app.config["SQLALCHEMY_DATABASE_URI"][10:]:
        db_path = os.path.join(app.instance_path, app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", ""))
        os.makedirs(app.instance_path, exist_ok=True)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path.replace("\\", "/")

    # Normalize postgres scheme
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("postgres://", "postgresql://", 1)

    app.config["LAST_ABACATE_RECONCILE_AT"] = 0.0

    db.init_app(app)

    with app.app_context():
        db.create_all()
        ensure_schema_for_sqlite(app.config["SQLALCHEMY_DATABASE_URI"])

        if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
            ensure_schema_for_postgres()
        seed_if_empty()

    def role_required(role: str):
        def decorator(fn):
            def wrapper(*args, **kwargs):
                if session.get("role") != role:
                    flash("Acesso restrito.", "danger")
                    return redirect(url_for("login", role=role))
                return fn(*args, **kwargs)
            wrapper.__name__ = fn.__name__
            return wrapper
        return decorator

    def cart_get():
        return session.get("cart", {})

    def cart_set(cart):
        session["cart"] = cart
        session.modified = True

    def cart_total(cart):
        total = Decimal("0")
        for pid, qty in cart.items():
            prod = Product.query.get(int(pid))
            if not prod or not prod.is_active:
                continue
            total += Decimal(str(prod.price)) * Decimal(str(qty))
        return total

    def last_order_id():
        return session.get("last_order_id")

    def session_cpf_hash():
        return session.get("cpf_hash")

    def maybe_reconcile_pending_orders(force: bool = False):
        if not _abacate_enabled(app):
            return 0
        now = time.time()
        last_run = float(app.config.get("LAST_ABACATE_RECONCILE_AT", 0.0) or 0.0)
        if not force and now - last_run < 20:
            return 0
        try:
            updated = _reconcile_pending_abacate_orders(app, limit=50)
            app.config["LAST_ABACATE_RECONCILE_AT"] = now
            return updated
        except Exception:
            db.session.rollback()
            return 0

    @app.before_request
    def run_pending_abacate_reconcile():
        path = (request.path or "")
        if path.startswith("/static/") or path == "/favicon.ico":
            return None
        if path.startswith("/webhooks/abacatepay"):
            return None
        maybe_reconcile_pending_orders()
        return None

    # ----------------------------
    # Landing / auth
    # ----------------------------
    @app.route("/")
    def index():
        return render_template("index.html", title=APP_TITLE, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/login/<role>", methods=["GET", "POST"])
    def login(role):
        role = role.lower()
        if role not in {"dono", "cozinha"}:
            return redirect(url_for("index"))

        if request.method == "POST":
            pin = (request.form.get("pin") or "").strip()
            pin_env = os.environ.get("PIN_DONO" if role == "dono" else "PIN_COZINHA",
                                     "9999" if role == "dono" else "1234")
            if pin == pin_env:
                session["role"] = role
                flash("Login realizado.", "success")
                return redirect(url_for("dono_dashboard" if role == "dono" else "cozinha_painel"))
            flash("PIN inválido.", "danger")

        return render_template("login.html", role=role, title=f"Login - {role}", cart=cart_get(), last_order_id=last_order_id())

    @app.route("/logout")
    def logout():
        session.pop("role", None)
        flash("Você saiu.", "info")
        return redirect(url_for("index"))

    # ----------------------------
    # Cliente (kiosk)
    # ----------------------------
    @app.route("/cliente/menu")
    def cliente_menu():
        products = Product.query.filter_by(is_active=True).order_by(Product.category, Product.name).all()
        categories = {}
        for p in products:
            categories.setdefault(p.category, []).append(p)
        return render_template("cliente_menu.html", title="Menu", categories=categories, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cliente/carrinho")
    def cliente_carrinho():
        cart = cart_get()
        items = []
        for pid, qty in cart.items():
            prod = Product.query.get(int(pid))
            if prod and prod.is_active:
                items.append({"product": prod, "qty": qty, "subtotal": float(prod.price) * int(qty)})
        total = float(cart_total(cart))
        return render_template("cliente_carrinho.html", title="Carrinho", items=items, total=total, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cliente/add/<int:product_id>", methods=["POST"])
    def cliente_add(product_id):
        qty = int(request.form.get("qty", "1"))
        qty = max(1, min(qty, 20))
        prod = Product.query.get_or_404(product_id)
        if not prod.is_active:
            flash("Produto indisponível.", "warning")
            return redirect(url_for("cliente_menu"))

        cart = cart_get()
        cart[str(product_id)] = int(cart.get(str(product_id), 0)) + qty
        cart_set(cart)
        flash(f"Adicionado: {prod.name}", "success")
        return redirect(url_for("cliente_menu"))

    @app.route("/cliente/remove/<int:product_id>", methods=["POST"])
    def cliente_remove(product_id):
        cart = cart_get()
        cart.pop(str(product_id), None)
        cart_set(cart)
        return redirect(url_for("cliente_carrinho"))

    @app.route("/cliente/update/<int:product_id>", methods=["POST"])
    def cliente_update(product_id):
        cart = cart_get()
        qty = int(request.form.get("qty", "1"))
        if qty <= 0:
            cart.pop(str(product_id), None)
        else:
            cart[str(product_id)] = min(qty, 20)
        cart_set(cart)
        return redirect(url_for("cliente_carrinho"))

    @app.route("/cliente/checkout", methods=["GET", "POST"])
    def cliente_checkout():
        cart = cart_get()
        if not cart:
            flash("Seu carrinho está vazio.", "warning")
            return redirect(url_for("cliente_menu"))

        if request.method == "POST":
            customer_name = (request.form.get("customer_name") or "").strip()[:60]
            table_number = (request.form.get("table_number") or "").strip()[:10]
            cpf_raw = (request.form.get("customer_cpf") or "").strip()
            customer_email = (request.form.get("customer_email") or "").strip()[:120]
            customer_cellphone = "".join(ch for ch in (request.form.get("customer_cellphone") or "") if ch.isdigit())[:13]
            payment_choice = (request.form.get("payment_choice") or "CAIXA").strip().upper()

            cpf_digits = _normalize_cpf(cpf_raw)
            if not customer_name:
                flash("Informe seu nome.", "danger")
                return redirect(url_for("cliente_checkout"))
            if not _is_valid_cpf(cpf_digits):
                flash("Informe um CPF válido.", "danger")
                return redirect(url_for("cliente_checkout"))
            if payment_choice not in {"CAIXA", "ABACATEPAY"}:
                flash("Forma de pagamento inválida.", "danger")
                return redirect(url_for("cliente_checkout"))
            if payment_choice == "ABACATEPAY" and (not customer_email or "@" not in customer_email):
                flash("Para pagar online, informe um e-mail válido.", "danger")
                return redirect(url_for("cliente_checkout"))

            total_dec = cart_total(cart)
            cpf_h = _cpf_hash(cpf_digits, app.config["SECRET_KEY"])
            cpf_last4 = cpf_digits[-4:]

            order = Order(
                created_at=datetime.utcnow(),
                customer_name=customer_name,
                table_number=table_number,
                status="AGUARDANDO_PAGAMENTO" if payment_choice == "ABACATEPAY" else "RECEBIDO",
                total=float(total_dec),
                is_paid=False,
                payment_method=None,
                paid_at=None,
                customer_email=customer_email or None,
                payment_gateway="ABACATEPAY" if payment_choice == "ABACATEPAY" else None,
                payment_status="PENDING" if payment_choice == "ABACATEPAY" else None,
                customer_cpf_hash=cpf_h,
                customer_cpf_last4=cpf_last4,
            )
            db.session.add(order)
            db.session.flush()

            for pid, qty in cart.items():
                prod = Product.query.get(int(pid))
                if not prod or not prod.is_active:
                    continue
                db.session.add(OrderItem(
                    order_id=order.id,
                    product_id=prod.id,
                    qty=int(qty),
                    unit_price=float(prod.price)
                ))
            db.session.commit()

            if payment_choice == "ABACATEPAY":
                try:
                    _create_abacate_checkout(order, customer_email, customer_cellphone, cpf_digits, app, request.host_url)
                except Exception as exc:
                    order.payment_gateway = None
                    order.payment_status = None
                    order.payment_external_id = None
                    order.payment_checkout_url = None
                    db.session.commit()
                    flash(f"Pedido criado, mas não foi possível iniciar o pagamento online: {exc}", "warning")

            cart_set({})
            session["last_order_id"] = order.id
            session["cpf_hash"] = cpf_h
            session.modified = True

            if payment_choice == "ABACATEPAY" and order.payment_checkout_url:
                flash("Pedido criado. Agora conclua o pagamento online pela AbacatePay. O pedido só vai para a cozinha após a confirmação do pagamento.", "success")
                return redirect(url_for("cliente_pagamento", order_id=order.id))

            flash("Pedido enviado para a cozinha! Você pode acompanhar o andamento.", "success")
            return redirect(url_for("cliente_status", order_id=order.id))

        total = float(cart_total(cart))
        return render_template("cliente_checkout.html", title="Checkout", total=total, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cliente/pedido", methods=["GET"])
    def cliente_acompanhar():
        return render_template("cliente_acompanhar.html", title="Acompanhar pedido", cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cliente/pedido/buscar", methods=["POST"])
    def cliente_acompanhar_buscar():
        cpf_digits = _normalize_cpf(request.form.get("customer_cpf") or "")
        if not _is_valid_cpf(cpf_digits):
            flash("Informe um CPF válido.", "danger")
            return redirect(url_for("cliente_acompanhar"))

        cpf_h = _cpf_hash(cpf_digits, app.config["SECRET_KEY"])
        session["cpf_hash"] = cpf_h
        session.modified = True

        orders = Order.query.filter(Order.customer_cpf_hash == cpf_h).order_by(Order.created_at.desc()).limit(10).all()
        if not orders:
            flash("Nenhum pedido encontrado para este CPF.", "warning")
            return redirect(url_for("cliente_acompanhar"))

        return render_template("cliente_lista_pedidos.html", title="Seus pedidos", orders=orders, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cliente/pedido/sair", methods=["POST"])
    def cliente_sair_cpf():
        session.pop("cpf_hash", None)
        flash("CPF removido. Informe novamente para acompanhar seus pedidos.", "info")
        return redirect(url_for("cliente_acompanhar"))




    @app.route("/cliente/pagamento/<int:order_id>")
    def cliente_pagamento(order_id):
        order = Order.query.get_or_404(order_id)
        cpf_h = session_cpf_hash()
        if not cpf_h or cpf_h != order.customer_cpf_hash:
            flash("Para ver este pagamento, informe seu CPF.", "warning")
            return redirect(url_for("cliente_acompanhar"))

        maybe_reconcile_pending_orders(force=True)
        _sync_order_payment_status(order, app)
        items = OrderItem.query.filter_by(order_id=order.id).all()
        return render_template(
            "cliente_pagamento.html",
            title="Pagamento online",
            order=order,
            items=items,
            abacate_enabled=_abacate_enabled(app),
            cart=cart_get(),
            last_order_id=last_order_id(),
        )

    @app.route("/cliente/pedido/<int:order_id>")
    def cliente_status(order_id):
        order = Order.query.get_or_404(order_id)

        cpf_h = session_cpf_hash()
        if not cpf_h or cpf_h != order.customer_cpf_hash:
            flash("Para ver este pedido, informe seu CPF.", "warning")
            return redirect(url_for("cliente_acompanhar"))

        maybe_reconcile_pending_orders(force=True)
        _sync_order_payment_status(order, app)
        items = OrderItem.query.filter_by(order_id=order.id).all()
        return render_template("cliente_status.html", title="Status do pedido", order=order, items=items, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/api/pedido/<int:order_id>")
    def api_pedido(order_id):
        order = Order.query.get_or_404(order_id)
        cpf_h = session_cpf_hash()
        if not cpf_h or cpf_h != order.customer_cpf_hash:
            return jsonify({"error": "unauthorized"}), 403

        maybe_reconcile_pending_orders(force=True)
        _sync_order_payment_status(order, app)
        return jsonify({
            "id": order.id,
            "status": order.status,
            "is_paid": bool(order.is_paid),
            "payment_method": order.payment_method,
            "payment_gateway": order.payment_gateway,
            "payment_status": order.payment_status,
            "payment_checkout_url": order.payment_checkout_url,
            "total": order.total,
        })



    @app.route("/webhooks/abacatepay", methods=["POST"])
    def webhook_abacatepay():
        configured_secret = app.config.get("ABACATEPAY_WEBHOOK_SECRET")
        request_secret = (request.args.get("webhookSecret") or "").strip()
        if configured_secret and request_secret != configured_secret:
            return jsonify({"error": "unauthorized"}), 401

        raw_body = request.get_data() or b""
        signature = request.headers.get("X-Webhook-Signature", "")
        if signature and not _verify_abacate_signature(raw_body, signature):
            return jsonify({"error": "invalid_signature"}), 401

        payload = request.get_json(silent=True) or {}
        event = (payload.get("event") or "").strip()
        data = payload.get("data") or {}

        # Compatibilidade com payloads v2 (checkout/payment) e legados (billing)
        entity = _normalize_abacate_entity(
            data.get("checkout")
            or data.get("payment")
            or data.get("billing")
            or data
            or {}
        )
        metadata = entity.get("metadata") or data.get("metadata") or {}

        external_id = (entity.get("externalId") or data.get("externalId") or metadata.get("externalId") or "").strip()
        order = _find_order_from_abacate_payload(entity=entity, data=data, metadata=metadata)

        if not order:
            logger = current_app.logger if current_app else app.logger
            logger.warning(
                "AbacatePay webhook sem pedido correspondente. event=%s entity_id=%s external_id=%s metadata=%s product_external_ids=%s payload=%s",
                event,
                entity.get("id"),
                external_id,
                metadata,
                [p.get("externalId") for p in (entity.get("products") or []) if isinstance(p, dict)],
                payload,
            )
            return jsonify({"ok": True}), 200

        merged_entity = {**entity, **(data if isinstance(data, dict) else {})}
        order.payment_gateway = "ABACATEPAY"
        order.payment_external_id = entity.get("id") or data.get("id") or order.payment_external_id
        order.payment_checkout_url = entity.get("url") or data.get("url") or order.payment_checkout_url
        order.payment_receipt_url = entity.get("receiptUrl") or entity.get("receipt_url") or data.get("receiptUrl") or data.get("receipt_url") or order.payment_receipt_url
        entity_status = (entity.get("status") or data.get("status") or order.payment_status or "").upper()
        if event == "billing.paid" and not entity_status:
            entity_status = "PAID"
        order.payment_status = entity_status or order.payment_status

        paid_events = {"checkout.completed", "payment.completed", "billing.paid"}
        if event in paid_events or entity_status == "PAID":
            _apply_paid_state(order, merged_entity)
        elif entity_status in {"EXPIRED", "CANCELLED", "CANCELED"} and not order.is_paid:
            _apply_cancelled_state(order, "CANCELLED" if entity_status in {"CANCELLED", "CANCELED"} else "EXPIRED")

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return jsonify({"ok": True}), 200

    # ----------------------------
    # Cozinha
    # ----------------------------
    @app.route("/cozinha")
    @role_required("cozinha")
    def cozinha_painel():
        status_filter = request.args.get("status", "").strip().upper()
        q = Order.query.filter(Order.status != "AGUARDANDO_PAGAMENTO").order_by(Order.created_at.desc())
        if status_filter:
            q = q.filter(Order.status == status_filter)
        orders = q.limit(50).all()

        order_ids = [o.id for o in orders]
        items = []
        if order_ids:
            items = OrderItem.query.filter(OrderItem.order_id.in_(order_ids)).all()
        order_items_map = {}
        for it in items:
            order_items_map.setdefault(it.order_id, []).append(it)
        return render_template("cozinha.html", title="Cozinha", orders=orders, order_items_map=order_items_map, status_filter=status_filter, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cozinha/pedido/<int:order_id>/status", methods=["POST"])
    @role_required("cozinha")
    def cozinha_update_status(order_id):
        order = Order.query.get_or_404(order_id)
        new_status = (request.form.get("status") or "").strip().upper()
        allowed = ["RECEBIDO", "EM_PREPARO", "PRONTO", "ENTREGUE"]
        if new_status not in allowed:
            flash("Status inválido.", "danger")
            return redirect(url_for("cozinha_painel"))
        order.status = new_status
        db.session.commit()
        flash(f"Pedido #{order.id} atualizado: {new_status}", "success")
        return redirect(url_for("cozinha_painel"))

    # ----------------------------
    # Dono / Financeiro / Caixa
    # ----------------------------
    @app.route("/dono")
    @role_required("dono")
    def dono_dashboard():
        month = (request.args.get("mes") or "").strip()
        if not month:
            month = date.today().strftime("%Y-%m")
        try:
            year, mon = [int(x) for x in month.split("-")]
            start = datetime(year, mon, 1)
            if mon == 12:
                end = datetime(year + 1, 1, 1)
            else:
                end = datetime(year, mon + 1, 1)
        except Exception:
            flash("Mês inválido. Use YYYY-MM", "danger")
            return redirect(url_for("dono_dashboard"))

        revenue = db.session.query(func.coalesce(func.sum(Order.total), 0.0)).filter(
            Order.created_at >= start,
            Order.created_at < end,
            Order.is_paid == True
        ).scalar() or 0.0

        expenses = db.session.query(func.coalesce(func.sum(Expense.amount), 0.0)).filter(
            Expense.date >= start.date(),
            Expense.date < end.date(),
        ).scalar() or 0.0

        profit = float(revenue) - float(expenses)

        recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()
        recent_expenses = Expense.query.order_by(Expense.date.desc(), Expense.id.desc()).limit(10).all()

        return render_template(
            "dono_dashboard.html",
            title="Painel do Dono",
            month=month,
            revenue=float(revenue),
            expenses=float(expenses),
            profit=float(profit),
            recent_orders=recent_orders,
            recent_expenses=recent_expenses,
            cart=cart_get(),
            last_order_id=last_order_id()
        )

    @app.route("/dono/pedidos")
    @role_required("dono")
    def dono_pedidos():
        orders = Order.query.order_by(Order.created_at.desc()).limit(200).all()
        return render_template("dono_pedidos.html", title="Pedidos", orders=orders, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/dono/pedidos/<int:order_id>")
    @role_required("dono")
    def dono_pedido_detalhe(order_id):
        order = Order.query.get_or_404(order_id)
        items = OrderItem.query.filter_by(order_id=order.id).all()
        return render_template("dono_pedido_detalhe.html", title=f"Pedido #{order.id}", order=order, items=items, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/dono/caixa/<int:order_id>", methods=["GET", "POST"])
    @role_required("dono")
    def dono_caixa(order_id):
        order = Order.query.get_or_404(order_id)
        items = OrderItem.query.filter_by(order_id=order.id).all()

        if request.method == "POST":
            method = (request.form.get("payment_method") or "").strip().upper()
            allowed = {"PIX", "CARTAO", "DINHEIRO"}
            if method not in allowed:
                flash("Forma de pagamento inválida.", "danger")
                return redirect(url_for("dono_caixa", order_id=order_id))

            order.is_paid = True
            order.payment_method = method
            order.paid_at = datetime.utcnow()
            db.session.commit()
            flash(f"Pagamento registrado: {method}.", "success")
            return redirect(url_for("dono_pedido_detalhe", order_id=order.id))

        return render_template("dono_caixa.html", title=f"Caixa • Pedido #{order.id}", order=order, items=items, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/dono/despesas", methods=["GET", "POST"])
    @role_required("dono")
    def dono_despesas():
        if request.method == "POST":
            desc = (request.form.get("description") or "").strip()[:120]
            amount = (request.form.get("amount") or "").replace(",", ".").strip()
            d = (request.form.get("date") or "").strip()
            try:
                amount_f = float(amount)
                if amount_f <= 0:
                    raise ValueError()
            except Exception:
                flash("Valor inválido.", "danger")
                return redirect(url_for("dono_despesas"))

            try:
                d_obj = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                d_obj = date.today()

            if not desc:
                desc = "Despesa"

            db.session.add(Expense(date=d_obj, description=desc, amount=amount_f))
            db.session.commit()
            flash("Despesa registrada.", "success")
            return redirect(url_for("dono_despesas"))

        items = Expense.query.order_by(Expense.date.desc(), Expense.id.desc()).limit(100).all()
        return render_template("dono_despesas.html", title="Despesas", items=items, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/dono/produtos")
    @role_required("dono")
    def dono_produtos():
        products = Product.query.order_by(Product.category, Product.name).all()
        return render_template("dono_produtos.html", title="Produtos", products=products, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/dono/produtos/<int:product_id>/toggle", methods=["POST"])
    @role_required("dono")
    def dono_produto_toggle(product_id):
        prod = Product.query.get_or_404(product_id)
        prod.is_active = not prod.is_active
        db.session.commit()
        flash("Produto atualizado.", "success")
        return redirect(url_for("dono_produtos"))

    return app


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(40), nullable=False)
    price = db.Column(db.Float, nullable=False)
    image_url = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, index=True)
    customer_name = db.Column(db.String(60), nullable=False)
    table_number = db.Column(db.String(10), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="RECEBIDO")
    total = db.Column(db.Float, nullable=False, default=0.0)

    is_paid = db.Column(db.Boolean, default=False)
    payment_method = db.Column(db.String(20), nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    customer_email = db.Column(db.String(120), nullable=True)
    payment_gateway = db.Column(db.String(30), nullable=True)
    payment_status = db.Column(db.String(30), nullable=True)
    payment_external_id = db.Column(db.String(80), nullable=True, index=True)
    payment_checkout_url = db.Column(db.String(500), nullable=True)
    payment_receipt_url = db.Column(db.String(500), nullable=True)

    customer_cpf_hash = db.Column(db.String(64), nullable=True, index=True)
    customer_cpf_last4 = db.Column(db.String(4), nullable=True)

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    qty = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Float, nullable=False)
    product = db.relationship("Product")

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    description = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Float, nullable=False)



def ensure_schema_for_postgres():
    """
    Migração leve para Postgres (Render) quando o banco já existe.
    create_all NÃO adiciona colunas novas em tabelas existentes.
    """
    try:
        stmts = [
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS is_paid BOOLEAN DEFAULT FALSE',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS payment_method VARCHAR(20)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS customer_cpf_hash VARCHAR(64)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS customer_cpf_last4 VARCHAR(4)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS customer_email VARCHAR(120)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS payment_gateway VARCHAR(30)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS payment_status VARCHAR(30)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS payment_external_id VARCHAR(80)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS payment_checkout_url VARCHAR(500)',
            'ALTER TABLE IF EXISTS "order" ADD COLUMN IF NOT EXISTS payment_receipt_url VARCHAR(500)',
            'CREATE INDEX IF NOT EXISTS ix_order_created_at ON "order" (created_at)',
            'CREATE INDEX IF NOT EXISTS ix_order_customer_cpf_hash ON "order" (customer_cpf_hash)',
            'CREATE INDEX IF NOT EXISTS ix_order_payment_external_id ON "order" (payment_external_id)',
        ]
        for s in stmts:
            db.session.execute(text(s))
        db.session.commit()
    except Exception:
        db.session.rollback()
        pass

def ensure_schema_for_sqlite(db_uri: str):
    if not (db_uri or "").startswith("sqlite"):
        return
    path = db_uri.replace("sqlite:///", "", 1)
    if not path or not os.path.exists(path):
        return
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("PRAGMA table_info('order')")
    cols = {row[1] for row in cur.fetchall()}
    alters = []
    if "is_paid" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN is_paid BOOLEAN DEFAULT 0")
    if "payment_method" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN payment_method VARCHAR(20)")
    if "paid_at" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN paid_at DATETIME")
    if "customer_cpf_hash" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN customer_cpf_hash VARCHAR(64)")
    if "customer_cpf_last4" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN customer_cpf_last4 VARCHAR(4)")
    if "customer_email" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN customer_email VARCHAR(120)")
    if "payment_gateway" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN payment_gateway VARCHAR(30)")
    if "payment_status" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN payment_status VARCHAR(30)")
    if "payment_external_id" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN payment_external_id VARCHAR(80)")
    if "payment_checkout_url" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN payment_checkout_url VARCHAR(500)")
    if "payment_receipt_url" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN payment_receipt_url VARCHAR(500)")
    for sql in alters:
        try:
            cur.execute(sql)
        except Exception:
            pass
    con.commit()
    con.close()


def seed_if_empty():
    if Product.query.count() > 0:
        return

    imgs = {
        "burger1": "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "burger2": "https://images.unsplash.com/photo-1572802419224-296b0aeee0d9?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "combo":   "https://images.unsplash.com/photo-1561758033-d89a9ad46330?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "burger4": "https://images.unsplash.com/photo-1550317138-10000687a72b?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "wings":   "https://images.unsplash.com/photo-1567620832903-9fc6debc209f?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "salad":   "https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "soda":    "https://images.unsplash.com/photo-1527960471264-932f39eb5846?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "coffee":  "https://plus.unsplash.com/premium_photo-1681711648620-9fa368907a86?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "milkshake":"https://images.unsplash.com/photo-1579954115545-a95591f28bfc?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
        "icecream":"https://images.unsplash.com/photo-1629385701021-fcd568a743e8?auto=format&fit=crop&fm=jpg&ixlib=rb-4.1.0&q=60&w=1200",
    }

    products = [
        Product(name="Big House Burger", category="Lanches", price=24.90, image_url=imgs["burger1"]),
        Product(name="Cheddar Duplo", category="Lanches", price=29.90, image_url=imgs["burger2"]),
        Product(name="Combo Burger + Fritas", category="Combos", price=39.90, image_url=imgs["combo"]),
        Product(name="Burger Bacon", category="Lanches", price=27.90, image_url=imgs["burger4"]),
        Product(name="Fritas Crocantes", category="Acompanhamentos", price=12.90, image_url=imgs["combo"]),
        Product(name="Onion Rings", category="Acompanhamentos", price=14.90, image_url=imgs["combo"]),
        Product(name="Chicken Wings", category="Acompanhamentos", price=22.90, image_url=imgs["wings"]),
        Product(name="Salada Fresh Bowl", category="Leves", price=19.90, image_url=imgs["salad"]),
        Product(name="Refrigerante Lata", category="Bebidas", price=6.90, image_url=imgs["soda"]),
        Product(name="Café Espresso", category="Bebidas", price=7.90, image_url=imgs["coffee"]),
        Product(name="Milkshake Morango", category="Bebidas", price=16.90, image_url=imgs["milkshake"]),
        Product(name="Sorvete Cone", category="Sobremesas", price=9.90, image_url=imgs["icecream"]),
    ]
    db.session.add_all(products)

    today = date.today()
    db.session.add_all([
        Expense(date=today.replace(day=1), description="Gás", amount=180.00),
        Expense(date=today.replace(day=min(5, today.day)), description="Embalagens", amount=120.00),
    ])
    db.session.commit()


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)

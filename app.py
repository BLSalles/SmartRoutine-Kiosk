import os
import sqlite3
from datetime import datetime, date
from decimal import Decimal

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

APP_TITLE = "SmartRoutine • Kiosk"

db = SQLAlchemy()


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Put relative sqlite db inside instance/
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///") and "://" not in app.config["SQLALCHEMY_DATABASE_URI"][10:]:
        db_path = os.path.join(app.instance_path, app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", ""))
        os.makedirs(app.instance_path, exist_ok=True)
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path.replace("\\", "/")

    db.init_app(app)

    with app.app_context():
        db.create_all()
        ensure_schema_for_sqlite()
        seed_if_empty()

    # ----------------------------
    # Helpers
    # ----------------------------
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
            if not customer_name:
                flash("Informe seu nome.", "danger")
                return redirect(url_for("cliente_checkout"))

            total_dec = cart_total(cart)

            order = Order(
                created_at=datetime.utcnow(),
                customer_name=customer_name,
                table_number=table_number,
                status="RECEBIDO",
                total=float(total_dec),
                is_paid=False,
                payment_method=None,
                paid_at=None,
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

            cart_set({})
            session["last_order_id"] = order.id
            flash("Pedido enviado para a cozinha! Você pode acompanhar o andamento.", "success")
            return redirect(url_for("cliente_status", order_id=order.id))

        total = float(cart_total(cart))
        return render_template("cliente_checkout.html", title="Checkout", total=total, cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cliente/pedido")
    def cliente_acompanhar():
        # If user already has a last order id in session, redirect
        oid = request.args.get("id") or session.get("last_order_id")
        if oid:
            try:
                return redirect(url_for("cliente_status", order_id=int(oid)))
            except Exception:
                pass
        return render_template("cliente_acompanhar.html", title="Acompanhar pedido", cart=cart_get(), last_order_id=last_order_id())

    @app.route("/cliente/pedido/<int:order_id>")
    def cliente_status(order_id):
        order = Order.query.get_or_404(order_id)
        items = OrderItem.query.filter_by(order_id=order.id).all()
        return render_template("cliente_status.html", title="Status do pedido", order=order, items=items, cart=cart_get(), last_order_id=last_order_id())

    # API for polling status (client)
    @app.route("/api/pedido/<int:order_id>")
    def api_pedido(order_id):
        order = Order.query.get_or_404(order_id)
        return jsonify({
            "id": order.id,
            "status": order.status,
            "is_paid": bool(order.is_paid),
            "payment_method": order.payment_method,
            "total": order.total,
        })

    # ----------------------------
    # Cozinha
    # ----------------------------
    @app.route("/cozinha")
    @role_required("cozinha")
    def cozinha_painel():
        status_filter = request.args.get("status", "").strip().upper()
        q = Order.query.order_by(Order.created_at.desc())
        if status_filter:
            q = q.filter(Order.status == status_filter)
        orders = q.limit(50).all()
        return render_template("cozinha.html", title="Cozinha", orders=orders, status_filter=status_filter, cart=cart_get(), last_order_id=last_order_id())

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

        # Revenue: consider PAID orders as revenue (caixa)
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

        # Recent orders show table/name/total/payment
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


# ----------------------------
# Models
# ----------------------------
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

    # Caixa / pagamento
    is_paid = db.Column(db.Boolean, default=False)
    payment_method = db.Column(db.String(20), nullable=True)  # PIX/CARTAO/DINHEIRO
    paid_at = db.Column(db.DateTime, nullable=True)

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


# ----------------------------
# SQLite schema helper (adds missing columns if you already have an old DB)
# ----------------------------
def ensure_schema_for_sqlite():
    uri = db.engine.url
    if uri.get_backend_name() != "sqlite":
        return

    db_path = uri.database
    if not db_path or not os.path.exists(db_path):
        return

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cur.execute("PRAGMA table_info('order')")
    cols = {row[1] for row in cur.fetchall()}

    # add columns if missing
    alters = []
    if "is_paid" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN is_paid BOOLEAN DEFAULT 0")
    if "payment_method" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN payment_method VARCHAR(20)")
    if "paid_at" not in cols:
        alters.append("ALTER TABLE 'order' ADD COLUMN paid_at DATETIME")

    for sql in alters:
        try:
            cur.execute(sql)
        except Exception:
            pass

    con.commit()
    con.close()


# ----------------------------
# Seed
# ----------------------------
def seed_if_empty():
    if Product.query.count() > 0:
        return

    # Unsplash images (hotlink) - 10+ items.
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

    # starter expenses
    today = date.today()
    example_expenses = [
        Expense(date=today.replace(day=1), description="Gás", amount=180.00),
        Expense(date=today.replace(day=min(5, today.day)), description="Embalagens", amount=120.00),
    ]
    db.session.add_all(example_expenses)
    db.session.commit()


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)

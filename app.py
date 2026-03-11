import os
import json
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from pywebpush import webpush, WebPushException

import psycopg2
from psycopg2.extras import RealDictCursor

APP_TZ_OFFSET = -3  # America/Sao_Paulo (UTC-03)

# Pegamos a URL do banco pelas variáveis de ambiente
DB_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("APP_SECRET_KEY", "troque-esta-chave-em-producao")

VAPID_SUBJECT = os.environ.get("APP_VAPID_SUBJECT", "mailto:admin@localhost")

app = Flask(__name__)
app.secret_key = SECRET_KEY

# -----------------------
# DB helpers (Adaptado para Postgres)
# -----------------------
class DBWrapper:
    """Um wrapper simples para fazer o psycopg2 se comportar parecido com o sqlite3"""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or ())
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

def conectar_db():
    if not DB_URL:
        raise ValueError("Variável DATABASE_URL não encontrada.")
    conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return DBWrapper(conn)

def agora_local():
    return (datetime.utcnow() + timedelta(hours=APP_TZ_OFFSET)).replace(microsecond=0)

def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")

def get_user(conn):
    uid = session.get("usuario_id")
    if not uid:
        return None
    return conn.execute("SELECT * FROM usuarios WHERE id=%s", (uid,)).fetchone()

def require_login():
    if "usuario_id" not in session:
        return False
    return True

def user_children(conn, usuario_id: int):
    return conn.execute("""
        SELECT c.*
        FROM criancas c
        JOIN familia_membros fm ON fm.crianca_id = c.id
        WHERE fm.usuario_id=%s
        ORDER BY c.id
    """, (usuario_id,)).fetchall()

def ensure_active_child(conn, usuario_id: int):
    cid = session.get("crianca_id")
    kids = user_children(conn, usuario_id)
    if not kids:
        return None
    if cid is None or not any(k["id"] == cid for k in kids):
        session["crianca_id"] = kids[0]["id"]
        cid = kids[0]["id"]
    return cid

def get_active_child(conn, usuario_id: int):
    cid = ensure_active_child(conn, usuario_id)
    if not cid:
        return None
    return conn.execute("SELECT * FROM criancas WHERE id=%s", (cid,)).fetchone()

def tem_acesso_crianca(conn, usuario_id: int, crianca_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM familia_membros WHERE usuario_id=%s AND crianca_id=%s", (usuario_id, crianca_id)).fetchone()
    return bool(row)

# -----------------------
# VAPID keys
# -----------------------
def get_vapid_keys(conn):
    priv_path = os.path.join(app.instance_path, "vapid_private.pem")
    pub = conn.execute("SELECT valor FROM app_config WHERE chave='vapid_public'").fetchone()
    pub_key = pub["valor"] if pub else None

    if os.path.exists(priv_path) and pub_key:
        return priv_path, pub_key

    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        import base64

        key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        private_bytes = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        pub_key = base64.urlsafe_b64encode(public_bytes).decode("utf-8").rstrip("=")

        os.makedirs(app.instance_path, exist_ok=True)
        with open(priv_path, "wb") as f:
            f.write(private_bytes)

        conn.execute("""
            INSERT INTO app_config (chave, valor) VALUES ('vapid_public', %s)
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
        """, (pub_key,))
        return priv_path, pub_key
    except Exception:
        return None, None

# -----------------------
# Static / PWA
# -----------------------
@app.route("/sw.js")
def serve_sw():
    return send_from_directory("static", "sw.js")

@app.route("/manifest.json")
def serve_manifest():
    return send_from_directory("static", "manifest.json")

# -----------------------
# Auth
# -----------------------
@app.route("/")
def index():
    if "usuario_id" in session:
        if session.get("role") == "crianca":
            return redirect(url_for("dashboard_crianca"))
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""

        conn = conectar_db()
        usuario = conn.execute("SELECT * FROM usuarios WHERE email=%s", (email,)).fetchone()

        ok = False
        if usuario:
            senha_hash = usuario["senha_hash"] or ""
            if senha_hash.startswith("PLAIN:"):
                plain = senha_hash.replace("PLAIN:", "", 1)
                ok = (senha == plain)
                if ok:
                    novo_hash = generate_password_hash(plain)
                    conn.execute("UPDATE usuarios SET senha_hash=%s WHERE id=%s", (novo_hash, usuario["id"]))
            else:
                ok = check_password_hash(senha_hash, senha)

        if ok:
            session["usuario_id"] = usuario["id"]
            session["nome"] = usuario["nome"]
            session["role"] = usuario["role"]
            
            if usuario["role"] == "crianca":
                link = conn.execute("SELECT crianca_id FROM familia_membros WHERE usuario_id=%s", (usuario["id"],)).fetchone()
                if link:
                    session["crianca_id"] = link["crianca_id"]
                conn.close()
                return redirect(url_for("dashboard_crianca"))
            
            ensure_active_child(conn, usuario["id"])
            conn.close()
            return redirect(url_for("dashboard"))

        conn.close()
        flash("Falha no login. Verifique e-mail e senha.", "error")

    return render_template("login.html")

@app.route("/registrar", methods=["GET", "POST"])
def registrar():
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""

        if not nome or not email or not senha:
            flash("Preencha nome, e-mail e senha.", "error")
            return redirect(url_for("registrar"))

        conn = conectar_db()
        try:
            senha_hash = generate_password_hash(senha)
            cur = conn.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, role) VALUES (%s,%s,%s,%s) RETURNING id",
                (nome, email, senha_hash, "responsavel")
            )
            uid = cur.fetchone()["id"]
            
            kid = conn.execute("SELECT id FROM criancas ORDER BY id LIMIT 1").fetchone()
            if kid:
                conn.execute("""
                    INSERT INTO familia_membros (usuario_id, crianca_id, papel) VALUES (%s,%s,%s)
                    ON CONFLICT (usuario_id, crianca_id) DO NOTHING
                """, (uid, kid["id"], "responsavel"))

            conn.close()
            flash("Conta criada! Faça login.", "ok")
            return redirect(url_for("login"))
        except psycopg2.IntegrityError:
            conn.close()
            flash("Este e-mail já está cadastrado.", "error")
            return redirect(url_for("registrar"))

    return render_template("registrar.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# -----------------------
# Dashboard Responsável
# -----------------------
@app.route("/dashboard")
def dashboard():
    if not require_login(): return redirect(url_for("login"))
    if session.get("role") == "crianca": return redirect(url_for("dashboard_crianca"))

    conn = conectar_db()
    user = get_user(conn)
    ativo = get_active_child(conn, user["id"])
    kids = user_children(conn, user["id"])

    hoje = agora_local().date().strftime("%Y-%m-%d")
    proximas = conn.execute("""
        SELECT * FROM tarefas
        WHERE crianca_id=%s AND status != 'Concluído' AND (data_entrega IS NULL OR data_entrega >= %s)
        ORDER BY COALESCE(data_entrega,'9999-12-31') ASC, COALESCE(hora_entrega,'23:59') ASC
        LIMIT 5
    """, (ativo["id"], hoje)).fetchall()

    crianca = conn.execute("SELECT * FROM criancas WHERE id=%s", (ativo["id"],)).fetchone()

    conn.close()
    return render_template("dashboard.html", usuario=user, kids=kids, ativo=ativo, crianca=crianca, proximas=proximas)

# -----------------------
# Dashboard Criança
# -----------------------
@app.route("/dashboard_crianca")
def dashboard_crianca():
    if not require_login() or session.get("role") != "crianca":
        return redirect(url_for("login"))

    conn = conectar_db()
    cid = session.get("crianca_id")
    
    crianca = conn.execute("SELECT * FROM criancas WHERE id=%s", (cid,)).fetchone()
    
    tarefas_db = conn.execute("""
        SELECT * FROM tarefas
        WHERE crianca_id=%s AND status != 'Concluído'
        ORDER BY COALESCE(data_entrega,'9999-12-31') ASC, id DESC
    """, (cid,)).fetchall()
    
    hoje = agora_local().date()
    tarefas_list = []
    for t in tarefas_db:
        tarefa = dict(t)
        if tarefa.get("data_entrega"):
            try:
                # Trata data_entrega caso retorne como objeto datetime.date do Postgres
                data_tarefa = tarefa["data_entrega"] if isinstance(tarefa["data_entrega"], datetime.date) else datetime.strptime(str(tarefa["data_entrega"]), "%Y-%m-%d").date()
                if data_tarefa < hoje:
                    tarefa["badge"] = {"text": "Atrasado", "cls": "bg-red-100 text-red-800"}
                else:
                    tarefa["badge"] = {"text": "A Fazer", "cls": "bg-slate-100 text-slate-800"}
            except:
                tarefa["badge"] = {"text": "A Fazer", "cls": "bg-slate-100 text-slate-800"}
        else:
            tarefa["badge"] = {"text": "A Fazer", "cls": "bg-slate-100 text-slate-800"}
            
        if tarefa["status"] == "Fazendo":
            tarefa["badge"] = {"text": "Fazendo", "cls": "bg-blue-100 text-blue-800"}
            
        tarefas_list.append(tarefa)

    conn.close()
    return render_template("dashboard_crianca.html", crianca=crianca, tarefas=tarefas_list)

# -----------------------
# Crianças / Perfis (CRUD)
# -----------------------
@app.route("/selecionar_crianca/<int:crianca_id>")
def selecionar_crianca(crianca_id):
    if not require_login(): return redirect(url_for("login"))
    conn = conectar_db()
    uid = session["usuario_id"]
    if tem_acesso_crianca(conn, uid, crianca_id):
        session["crianca_id"] = crianca_id
    conn.close()
    return redirect(request.referrer or url_for("dashboard"))

@app.route("/criancas", methods=["GET", "POST"])
def criancas():
    if not require_login(): return redirect(url_for("login"))
    if session.get("role") == "crianca": return redirect(url_for("dashboard_crianca"))

    conn = conectar_db()
    uid = session["usuario_id"]

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        apelido = (request.form.get("apelido") or "").strip()
        email_crianca = (request.form.get("email_crianca") or "").strip().lower()
        senha_crianca = request.form.get("senha_crianca") or ""

        if nome and email_crianca and senha_crianca:
            try:
                senha_hash = generate_password_hash(senha_crianca)
                cur = conn.execute(
                    "INSERT INTO usuarios (nome, email, senha_hash, role) VALUES (%s,%s,%s,%s) RETURNING id",
                    (nome, email_crianca, senha_hash, "crianca")
                )
                uid_crianca = cur.fetchone()["id"]

                cur = conn.execute("INSERT INTO criancas (nome, apelido) VALUES (%s,%s) RETURNING id", (nome, apelido or None))
                cid = cur.fetchone()["id"]

                conn.execute("INSERT INTO familia_membros (usuario_id, crianca_id, papel) VALUES (%s,%s,%s)", (uid, cid, "responsavel"))
                conn.execute("INSERT INTO familia_membros (usuario_id, crianca_id, papel) VALUES (%s,%s,%s)", (uid_crianca, cid, "crianca"))

                session["crianca_id"] = cid
                flash("Perfil e login criados com sucesso!", "ok")
            except psycopg2.IntegrityError:
                flash("Este e-mail/usuário já está em uso.", "error")
        else:
            flash("Preencha o nome, e-mail de acesso e a senha.", "error")

    kids = user_children(conn, uid)
    ativo = get_active_child(conn, uid)
    conn.close()
    return render_template("criancas.html", kids=kids, ativo=ativo)

@app.route("/criancas/<int:crianca_id>/editar", methods=["GET", "POST"])
def editar_crianca(crianca_id):
    if not require_login(): return redirect(url_for("login"))
    if session.get("role") == "crianca": return redirect(url_for("dashboard_crianca"))

    conn = conectar_db()
    uid = session["usuario_id"]

    if not tem_acesso_crianca(conn, uid, crianca_id):
        conn.close()
        flash("Acesso negado.", "error")
        return redirect(url_for("criancas"))

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        apelido = (request.form.get("apelido") or "").strip()
        nova_senha = request.form.get("nova_senha") or ""
        
        if nome:
            conn.execute("UPDATE criancas SET nome=%s, apelido=%s WHERE id=%s", (nome, apelido or None, crianca_id))
            
            if nova_senha:
                link = conn.execute("SELECT usuario_id FROM familia_membros WHERE crianca_id=%s AND papel='crianca'", (crianca_id,)).fetchone()
                if link:
                    senha_hash = generate_password_hash(nova_senha)
                    conn.execute("UPDATE usuarios SET senha_hash=%s WHERE id=%s", (senha_hash, link["usuario_id"]))

            flash("Perfil atualizado.", "ok")
            conn.close()
            return redirect(url_for("criancas"))

    crianca = conn.execute("SELECT * FROM criancas WHERE id=%s", (crianca_id,)).fetchone()
    login_info = conn.execute("""
        SELECT u.email FROM usuarios u
        JOIN familia_membros fm ON fm.usuario_id = u.id
        WHERE fm.crianca_id = %s AND fm.papel = 'crianca'
    """, (crianca_id,)).fetchone()
    
    email_crianca = login_info["email"] if login_info else None

    conn.close()
    return render_template("editar_crianca.html", crianca=crianca, email_crianca=email_crianca)

@app.route("/criancas/<int:crianca_id>/deletar")
def deletar_crianca(crianca_id):
    if not require_login(): return redirect(url_for("login"))
    if session.get("role") == "crianca": return redirect(url_for("dashboard_crianca"))

    conn = conectar_db()
    uid = session["usuario_id"]

    if tem_acesso_crianca(conn, uid, crianca_id):
        link = conn.execute("SELECT usuario_id FROM familia_membros WHERE crianca_id=%s AND papel='crianca'", (crianca_id,)).fetchone()
        
        # Exclusão no Postgres com CASCADE resolve quase tudo, mas para os usuários precisamos apagar manualmente
        conn.execute("DELETE FROM criancas WHERE id=%s", (crianca_id,))
        if link:
            conn.execute("DELETE FROM usuarios WHERE id=%s", (link["usuario_id"],))
        
        if session.get("crianca_id") == crianca_id:
            session.pop("crianca_id", None)
            ensure_active_child(conn, uid)

        flash("Perfil e tarefas removidos.", "ok")
        
    conn.close()
    return redirect(url_for("criancas"))

# -----------------------
# Tarefas (CRUD)
# -----------------------
@app.route("/tarefas")
def tarefas():
    if not require_login(): return redirect(url_for("login"))
    if session.get("role") == "crianca": return redirect(url_for("dashboard_crianca"))

    conn = conectar_db()
    uid = session["usuario_id"]
    ativo = get_active_child(conn, uid)
    kids = user_children(conn, uid)

    filtro_status = request.args.get("status", "todas")
    filtro_tipo = request.args.get("tipo", "todos")

    where = ["crianca_id=%s"]
    params = [ativo["id"]]

    if filtro_status != "todas":
        where.append("status=%s")
        params.append(filtro_status)

    if filtro_tipo != "todos":
        where.append("tipo=%s")
        params.append(filtro_tipo)

    sql = f"""
        SELECT * FROM tarefas
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE status
            WHEN 'A Fazer' THEN 1
            WHEN 'Fazendo' THEN 2
            WHEN 'Concluído' THEN 3
            ELSE 9
          END,
          COALESCE(data_entrega,'9999-12-31') ASC,
          COALESCE(hora_entrega,'23:59') ASC,
          id DESC
    """
    tarefas_db = conn.execute(sql, tuple(params)).fetchall()

    hoje = agora_local().date()
    tarefas_concluidas = 0
    tarefas_list = []
    for t in tarefas_db:
        tarefa = dict(t)
        if tarefa.get("data_entrega"):
            try:
                data_tarefa = tarefa["data_entrega"] if isinstance(tarefa["data_entrega"], datetime.date) else datetime.strptime(str(tarefa["data_entrega"]), "%Y-%m-%d").date()
            except Exception:
                data_tarefa = None
        else:
            data_tarefa = None

        if tarefa["status"] == "Concluído":
            tarefa["badge"] = {"text": "Concluído", "cls": "bg-green-100 text-green-800"}
            tarefas_concluidas += 1
        elif tarefa["status"] == "Fazendo":
            tarefa["badge"] = {"text": "Fazendo", "cls": "bg-blue-100 text-blue-800"}
        else:
            if data_tarefa and data_tarefa < hoje:
                tarefa["badge"] = {"text": "Atrasado", "cls": "bg-red-100 text-red-800"}
            else:
                tarefa["badge"] = {"text": "A Fazer", "cls": "bg-slate-100 text-slate-800"}

        tarefas_list.append(tarefa)

    total = len(tarefas_db)
    porcentagem = int((tarefas_concluidas / total) * 100) if total else 0

    conn.close()
    return render_template(
        "tarefas.html",
        kids=kids,
        ativo=ativo,
        tarefas=tarefas_list,
        concluidas=tarefas_concluidas,
        total=total,
        porcentagem=porcentagem,
        filtro_status=filtro_status,
        filtro_tipo=filtro_tipo,
    )

@app.route("/tarefas/adicionar", methods=["POST"])
def adicionar_tarefa():
    if not require_login(): return redirect(url_for("login"))

    conn = conectar_db()
    uid = session["usuario_id"]
    ativo = get_active_child(conn, uid)

    titulo = (request.form.get("titulo") or "").strip()
    tipo = (request.form.get("tipo") or "Casa").strip()
    disciplina = (request.form.get("disciplina") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    prioridade = (request.form.get("prioridade") or "Media").strip()
    data = (request.form.get("data_entrega") or "").strip() or None
    hora = (request.form.get("hora_entrega") or "").strip() or None
    lembrete = (request.form.get("lembrete_em") or "").strip() or None
    xp = int(request.form.get("xp_recompensa") or 50)

    if not titulo:
        flash("Informe o título da tarefa.", "error")
        conn.close()
        return redirect(url_for("tarefas"))

    conn.execute("""
        INSERT INTO tarefas (crianca_id, criado_por, titulo, tipo, disciplina, descricao, prioridade,
                            data_entrega, hora_entrega, lembrete_em, status, xp_recompensa, criado_em, atualizado_em)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        ativo["id"], uid, titulo, tipo, disciplina or None, descricao or None, prioridade,
        data, hora, lembrete, "A Fazer", xp, iso(agora_local()), iso(agora_local())
    ))
    conn.close()
    flash("Tarefa adicionada.", "ok")
    return redirect(url_for("tarefas"))

@app.route("/tarefas/<int:tarefa_id>/status/<novo_status>")
def atualizar_status(tarefa_id, novo_status):
    if not require_login(): return redirect(url_for("login"))

    if novo_status not in ("A Fazer", "Fazendo", "Concluído"):
        flash("Status inválido.", "error")
        return redirect(request.referrer or url_for("tarefas"))

    conn = conectar_db()
    uid = session["usuario_id"]
    ativo = get_active_child(conn, uid)

    tarefa = conn.execute("SELECT * FROM tarefas WHERE id=%s AND crianca_id=%s", (tarefa_id, ativo["id"])).fetchone()
    if not tarefa:
        conn.close()
        flash("Tarefa não encontrada.", "error")
        return redirect(request.referrer or url_for("tarefas"))

    if novo_status == "Concluído" and tarefa["status"] != "Concluído":
        conn.execute("UPDATE criancas SET xp_total = xp_total + %s WHERE id=%s", (tarefa["xp_recompensa"], ativo["id"]))
        crianca = conn.execute("SELECT xp_total, nivel FROM criancas WHERE id=%s", (ativo["id"],)).fetchone()
        novo_nivel = (crianca["xp_total"] // 100) + 1
        if novo_nivel > crianca["nivel"]:
            conn.execute("UPDATE criancas SET nivel=%s WHERE id=%s", (novo_nivel, ativo["id"]))

    conn.execute("UPDATE tarefas SET status=%s, atualizado_em=%s WHERE id=%s", (novo_status, iso(agora_local()), tarefa_id))
    conn.close()
    
    return redirect(request.referrer or url_for("tarefas"))

@app.route("/tarefas/<int:tarefa_id>/deletar")
def deletar_tarefa(tarefa_id):
    if not require_login(): return redirect(url_for("login"))

    conn = conectar_db()
    uid = session["usuario_id"]
    ativo = get_active_child(conn, uid)

    conn.execute("DELETE FROM tarefas WHERE id=%s AND crianca_id=%s", (tarefa_id, ativo["id"]))
    conn.close()
    flash("Tarefa removida.", "ok")
    return redirect(url_for("tarefas"))

# -----------------------
# Perfil
# -----------------------
@app.route("/perfil")
def perfil():
    if not require_login(): return redirect(url_for("login"))
    if session.get("role") == "crianca": return redirect(url_for("dashboard_crianca"))

    conn = conectar_db()
    user = get_user(conn)
    ativo = get_active_child(conn, user["id"])
    kids = user_children(conn, user["id"])

    total_concluidas = conn.execute("""
        SELECT COUNT(*) AS t FROM tarefas
        WHERE crianca_id=%s AND status='Concluído'
    """, (ativo["id"],)).fetchone()["t"]

    conn.close()
    return render_template("perfil.html", usuario=user, kids=kids, ativo=ativo, concluidas=total_concluidas)


@app.route("/adicionar_responsavel", methods=["POST"])
def adicionar_responsavel():
    if not require_login(): return redirect(url_for("login"))
    if session.get("role") == "crianca": return redirect(url_for("dashboard_crianca"))

    conn = conectar_db()
    uid_atual = session["usuario_id"]

    nome = (request.form.get("nome") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    senha = request.form.get("senha") or ""

    if not nome or not email or not senha:
        flash("Preencha todos os campos para adicionar o responsável.", "error")
        conn.close()
        return redirect(url_for("perfil"))

    try:
        # 1. Cria o novo usuário como 'responsavel'
        senha_hash = generate_password_hash(senha)
        cur = conn.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, role) VALUES (%s,%s,%s,%s) RETURNING id",
            (nome, email, senha_hash, "responsavel")
        )
        novo_uid = cur.fetchone()["id"]

        # 2. Pega as crianças do usuário atual e vincula ao novo responsável
        kids = user_children(conn, uid_atual)
        for k in kids:
            conn.execute("""
                INSERT INTO familia_membros (usuario_id, crianca_id, papel) VALUES (%s,%s,%s)
                ON CONFLICT (usuario_id, crianca_id) DO NOTHING
            """, (novo_uid, k["id"], "responsavel"))

        flash(f"Responsável {nome} adicionado com sucesso! Já pode fazer login.", "ok")
    except psycopg2.IntegrityError:
        flash("Este e-mail já está em uso por outro usuário.", "error")
    except Exception:
        flash("Erro ao adicionar responsável.", "error")

    conn.close()
    return redirect(url_for("perfil"))

# -----------------------
# Push subscribe endpoints
# -----------------------
@app.route("/push/public_key")
def push_public_key():
    if not require_login(): return jsonify({"error": "unauthorized"}), 401
    conn = conectar_db()
    priv, pub = get_vapid_keys(conn)
    conn.close()
    if not pub: return jsonify({"enabled": False})
    return jsonify({"enabled": True, "publicKey": pub})

@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    if not require_login(): return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    sub = payload.get("subscription") or {}
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "invalid subscription"}), 400

    conn = conectar_db()
    uid = session["usuario_id"]
    ativo = get_active_child(conn, uid)

    conn.execute("""
        INSERT INTO push_subscriptions (usuario_id, crianca_id, endpoint, p256dh, auth, user_agent)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (endpoint) DO UPDATE SET
            p256dh = EXCLUDED.p256dh,
            auth = EXCLUDED.auth,
            user_agent = EXCLUDED.user_agent,
            usuario_id = EXCLUDED.usuario_id,
            crianca_id = EXCLUDED.crianca_id
    """, (uid, ativo["id"], endpoint, p256dh, auth, request.headers.get("User-Agent", "")))
    
    conn.close()
    return jsonify({"ok": True})

@app.route("/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    if not require_login(): return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    endpoint = (payload.get("endpoint") or "").strip()
    if not endpoint: return jsonify({"error": "invalid"}), 400

    conn = conectar_db()
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (endpoint,))
    conn.close()
    return jsonify({"ok": True})

# -----------------------
# Lembretes (Scheduler)
# -----------------------
def enviar_push_para_crianca(conn, crianca_id: int, titulo: str, corpo: str):
    priv, pub = get_vapid_keys(conn)
    if not priv or not pub: return 0

    subs = conn.execute("SELECT * FROM push_subscriptions WHERE crianca_id=%s", (crianca_id,)).fetchall()
    enviados = 0
    for s in subs:
        subscription_info = {
            "endpoint": s["endpoint"],
            "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
        }
        try:
            webpush(
                subscription_info,
                data=json.dumps({"title": titulo, "body": corpo}),
                vapid_private_key=priv,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            enviados += 1
        except WebPushException:
            conn.execute("DELETE FROM push_subscriptions WHERE id=%s", (s["id"],))
        except Exception:
            continue
    return enviados

def job_lembretes():
    try:
        conn = conectar_db()
        agora = agora_local()
        janela_inicio = agora - timedelta(seconds=30)
        janela_fim = agora + timedelta(seconds=30)

        rows = conn.execute("""
            SELECT * FROM tarefas
            WHERE lembrete_em IS NOT NULL
              AND lembrete_enviado=0
              AND lembrete_em >= %s
              AND lembrete_em <= %s
        """, (iso(janela_inicio), iso(janela_fim))).fetchall()

        for t in rows:
            titulo = f"Lembrete: {t['titulo']}"
            corpo = f"{t['tipo']}" + (f" • {t['disciplina']}" if t["disciplina"] else "")
            if t["data_entrega"]:
                corpo += f" • Entrega: {t['data_entrega']}" + (f" {t['hora_entrega']}" if t["hora_entrega"] else "")
            enviar_push_para_crianca(conn, t["crianca_id"], titulo, corpo)

            conn.execute("UPDATE tarefas SET lembrete_enviado=1, atualizado_em=%s WHERE id=%s", (iso(agora_local()), t["id"]))

        conn.close()
    except Exception:
        pass

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(job_lembretes, "interval", seconds=30)

@app.before_request
def _start_scheduler_once():
    if not getattr(app, "_scheduler_started", False):
        scheduler.start()
        app._scheduler_started = True

# -----------------------
# Main
# -----------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
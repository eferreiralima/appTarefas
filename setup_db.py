import os
import psycopg2
from psycopg2.extras import RealDictCursor

# Pegamos a URL do Supabase pelas variáveis de ambiente
DB_URL = os.environ.get("DATABASE_URL")

def connect():
    if not DB_URL:
        raise ValueError("A variável de ambiente DATABASE_URL não está configurada.")
    # Usamos RealDictCursor para que o retorno aja como dicionários (igual ao sqlite3.Row)
    conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
    # Autocommit facilita a criação de tabelas
    conn.autocommit = True 
    return conn

def inicializar_banco():
    print("Conectando ao Supabase...")
    conn = connect()
    cur = conn.cursor()

    # 1) Usuarios (responsáveis e crianças)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            nome VARCHAR NOT NULL,
            email VARCHAR UNIQUE NOT NULL,
            senha_hash VARCHAR NOT NULL,
            role VARCHAR NOT NULL DEFAULT 'responsavel',
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2) Crianças (gamificação)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS criancas (
            id SERIAL PRIMARY KEY,
            nome VARCHAR NOT NULL,
            apelido VARCHAR,
            xp_total INTEGER DEFAULT 0,
            nivel INTEGER DEFAULT 1,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3) Vínculo usuário <-> criança
    cur.execute("""
        CREATE TABLE IF NOT EXISTS familia_membros (
            id SERIAL PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            crianca_id INTEGER NOT NULL REFERENCES criancas(id) ON DELETE CASCADE,
            papel VARCHAR NOT NULL DEFAULT 'responsavel',
            UNIQUE(usuario_id, crianca_id)
        )
    """)

    # 4) Tarefas
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tarefas (
            id SERIAL PRIMARY KEY,
            crianca_id INTEGER NOT NULL REFERENCES criancas(id) ON DELETE CASCADE,
            criado_por INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            titulo VARCHAR NOT NULL,
            tipo VARCHAR NOT NULL DEFAULT 'Casa',
            disciplina VARCHAR,
            descricao TEXT,
            prioridade VARCHAR NOT NULL DEFAULT 'Media',
            data_entrega DATE,
            hora_entrega TIME,
            lembrete_em TIMESTAMP,
            lembrete_enviado INTEGER DEFAULT 0,
            status VARCHAR NOT NULL DEFAULT 'A Fazer',
            xp_recompensa INTEGER DEFAULT 50,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TIMESTAMP
        )
    """)

    # 5) Web Push subscriptions
    cur.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            crianca_id INTEGER NOT NULL REFERENCES criancas(id) ON DELETE CASCADE,
            endpoint VARCHAR UNIQUE NOT NULL,
            p256dh VARCHAR NOT NULL,
            auth VARCHAR NOT NULL,
            user_agent VARCHAR,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 6) Config
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            chave VARCHAR PRIMARY KEY,
            valor VARCHAR
        )
    """)

    print("Tabelas criadas/verificadas com sucesso!")

    # --- DADOS INICIAIS (SEED) ---
    # Garante criança padrão
    cur.execute("SELECT id FROM criancas ORDER BY id LIMIT 1")
    child = cur.fetchone()
    if not child:
        cur.execute("INSERT INTO criancas (nome, apelido) VALUES (%s, %s) RETURNING id", ("Minha Filha", "Filha"))
        child = cur.fetchone()

    # Garante usuário responsável padrão
    cur.execute("SELECT id FROM usuarios WHERE role='responsavel' ORDER BY id LIMIT 1")
    user = cur.fetchone()
    if not user:
        cur.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, role) VALUES (%s,%s,%s,%s) RETURNING id",
            ("Responsável", "responsavel@familia.com", "PLAIN:123456", "responsavel")
        )
        user = cur.fetchone()

    # Vincula o usuário padrão à criança padrão
    cur.execute("""
        INSERT INTO familia_membros (usuario_id, crianca_id, papel) 
        VALUES (%s, %s, %s) 
        ON CONFLICT (usuario_id, crianca_id) DO NOTHING
    """, (user["id"], child["id"], "responsavel"))

    conn.close()
    print("Banco de dados no Supabase pronto para uso!")

if __name__ == "__main__":
    inicializar_banco()
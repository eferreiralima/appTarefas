# App de Tarefas (Pai/Mãe + Filha) – Flask + SQLite + PWA

## O que está pronto
- Login (pai/mãe/responsável) com senha **hash**
- Perfis de crianças (você pode criar mais de um)
- Tarefas por criança, com status: **A Fazer / Fazendo / Concluído**
- XP/Nível por criança (sobe 1 nível a cada 100 XP)
- PWA (instalável) + **Push Notifications** (se o navegador permitir)

> Observação: notificações Push dependem do navegador/permissões e de HTTPS em produção.

---

## Como rodar (Windows / Linux)
1) Crie um venv e instale dependências:
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
```

2) Inicialize/atualize o banco:
```bash
python setup_db.py
```

3) Rode o app:
```bash
python app.py
```

Acesse: http://127.0.0.1:5000

### Login padrão (para começar rápido)
- Email: **responsavel@familia.com**
- Senha: **123456**

---

## Notificações (Push)
1) Entre no app
2) Clique **“Ativar notificações”**
3) Dê permissão ao navegador

Depois, em uma tarefa, preencha o campo **Lembrete** (data/hora).

> O servidor verifica lembretes a cada ~30 segundos.

---

## Variáveis de ambiente (opcional)
- `APP_DB` (caminho do banco; padrão `banco.db`)
- `APP_SECRET_KEY` (recomendado trocar em produção)
- `PORT` (porta do Flask; padrão 5000)

---

## Estrutura
- `app.py` (Flask)
- `setup_db.py` (migração/criação do SQLite)
- `templates/` (HTML)
- `static/` (PWA: sw.js, manifest, ícones)

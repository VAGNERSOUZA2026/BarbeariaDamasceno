import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Barbearia Damasceno | Agendamento", page_icon="ðŸ’ˆ", layout="centered")

# ALTERE OS SERVIÃ‡OS E VALORES AQUI.
SERVICOS = {
    "Corte masculino": (35.0, 30),
    "Barba": (25.0, 30),
    "Corte + barba": (55.0, 60),
    "Sobrancelha": (15.0, 30),
}
# 0 = segunda-feira; 6 = domingo. Exemplo: segunda a sÃ¡bado.
DIAS_ATENDIMENTO = {0, 1, 2, 3, 4, 5}
ABRE = time(9, 0)
FECHA = time(18, 0)
INTERVALO_MINUTOS = 30

CSS = """
<style>
.stApp {background: #111317; color: #f5f5f5;}
.block-container {max-width: 720px; padding-top: 2rem;}
.hero {background: linear-gradient(130deg,#272a30,#141518); border:1px solid #3b3d40;
       border-radius:22px; padding:26px 22px; margin-bottom:25px; text-align:center;}
.hero h1 {color:#f4c45e; font-size:2.2rem; margin:6px 0 2px;}
.hero p {color:#dedede; margin:0;}
[data-testid="stForm"] {border:1px solid #393c41; background:#1e2025; border-radius:18px; padding:22px;}
div.stButton > button[kind="primary"], button[kind="formSubmit"] {background:#c79c43; color:#111; font-weight:700; border:0;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def secret_or_env(name, default=""):
    try:
        return str(st.secrets.get(name, os.environ.get(name, default)))
    except Exception:
        return str(os.environ.get(name, default))


DB_PATH = Path(secret_or_env("DB_PATH", "agendamentos.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def conectar():
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.execute("PRAGMA busy_timeout=15000")
    try:
        yield c
    finally:
        c.close()


def iniciar_banco():
    with conectar() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS agendamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            telefone TEXT NOT NULL,
            servico TEXT NOT NULL,
            preco REAL NOT NULL,
            data TEXT NOT NULL,
            horario TEXT NOT NULL,
            duracao INTEGER NOT NULL,
            criado_em TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Confirmado'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ocupacao (
            data TEXT NOT NULL,
            horario TEXT NOT NULL,
            agendamento_id INTEGER NOT NULL,
            PRIMARY KEY (data, horario)
        )""")


def blocos(inicio, duracao):
    base = datetime.combine(date.today(), inicio)
    return [(base + timedelta(minutes=m)).strftime("%H:%M")
            for m in range(0, duracao, INTERVALO_MINUTOS)]


def todos_horarios(dia, duracao):
    if dia.weekday() not in DIAS_ATENDIMENTO:
        return []
    abertura = datetime.combine(dia, ABRE)
    fechamento = datetime.combine(dia, FECHA)
    agora = datetime.now()
    with conectar() as c:
        ocupados = {r[0] for r in c.execute("SELECT horario FROM ocupacao WHERE data=?", (dia.isoformat(),))}
    horarios = []
    inicio = abertura
    while inicio + timedelta(minutes=duracao) <= fechamento:
        livre = all(h not in ocupados for h in blocos(inicio.time(), duracao))
        # NÃ£o permite agendar horÃ¡rios passados ou com menos de 30 minutos de antecedÃªncia.
        if livre and inicio >= agora + timedelta(minutes=30):
            horarios.append(inicio.strftime("%H:%M"))
        inicio += timedelta(minutes=INTERVALO_MINUTOS)
    return horarios


def reservar(nome, telefone, servico, dia, horario):
    preco, duracao = SERVICOS[servico]
    slots = blocos(time.fromisoformat(horario), duracao)
    with conectar() as c:
        try:
            c.execute("BEGIN IMMEDIATE")
            ocupados = {r[0] for r in c.execute("SELECT horario FROM ocupacao WHERE data=?", (dia.isoformat(),))}
            inicio = datetime.combine(dia, time.fromisoformat(horario))
            if dia.weekday() not in DIAS_ATENDIMENTO or inicio < datetime.now() + timedelta(minutes=30):
                c.execute("ROLLBACK")
                return False
            if inicio.time() < ABRE or inicio + timedelta(minutes=duracao) > datetime.combine(dia, FECHA):
                c.execute("ROLLBACK")
                return False
            if any(h in ocupados for h in slots):
                c.execute("ROLLBACK")
                return False
            cur = c.execute("""INSERT INTO agendamentos
                (nome, telefone, servico, preco, data, horario, duracao, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (nome, telefone, servico, preco, dia.isoformat(), horario, duracao, datetime.now().isoformat(timespec="seconds")))
            c.executemany("INSERT INTO ocupacao (data, horario, agendamento_id) VALUES (?, ?, ?)",
                          [(dia.isoformat(), h, cur.lastrowid) for h in slots])
            c.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            c.execute("ROLLBACK")
            return False
        except Exception:
            c.execute("ROLLBACK")
            raise


def cancelar(identificador):
    with conectar() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM ocupacao WHERE agendamento_id=?", (identificador,))
        c.execute("UPDATE agendamentos SET status='Cancelado' WHERE id=?", (identificador,))
        c.execute("COMMIT")


iniciar_banco()
st.markdown('<div class="hero"><div style="font-size:2.8rem">ðŸ’ˆ</div><h1>Barbearia Damasceno</h1><p>Seu estilo comeÃ§a aqui. Agende em poucos cliques.</p></div>', unsafe_allow_html=True)

aba_cliente, aba_admin = st.tabs(["ðŸ“… Agendar horÃ¡rio", "ðŸ”’ AdministraÃ§Ã£o"])

with aba_cliente:
    st.subheader("Escolha seu atendimento")
    servico = st.selectbox("ServiÃ§o", list(SERVICOS))
    preco, duracao = SERVICOS[servico]
    st.caption(f"DuraÃ§Ã£o: {duracao} minutos  â€¢  Valor: R$ {preco:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    hoje = date.today()
    dia = st.date_input("Escolha o dia", min_value=hoje, max_value=hoje + timedelta(days=60), value=hoje, format="DD/MM/YYYY")
    opcoes = todos_horarios(dia, duracao)
    if not opcoes:
        st.warning("NÃ£o hÃ¡ horÃ¡rios disponÃ­veis nesse dia. Escolha outra data.")
    else:
        horario = st.selectbox("HorÃ¡rios disponÃ­veis", opcoes)
        with st.form("form_cliente", clear_on_submit=False):
            nome = st.text_input("Seu nome", max_chars=90)
            telefone = st.text_input("WhatsApp (com DDD)", max_chars=20, placeholder="31999999999")
            enviar = st.form_submit_button("Confirmar agendamento", use_container_width=True)
        if enviar:
            digitos = "".join(ch for ch in telefone if ch.isdigit())
            if len(nome.strip()) < 2 or len(digitos) not in (10, 11):
                st.error("Informe seu nome e um telefone com DDD vÃ¡lido.")
            elif reservar(nome.strip(), digitos, servico, dia, horario):
                st.success(f"Agendamento confirmado! {servico}, {dia.strftime('%d/%m/%Y')} Ã s {horario}.")
                st.balloons()
            else:
                st.error("Esse horÃ¡rio acabou de ser reservado ou nÃ£o estÃ¡ mais disponÃ­vel. Selecione outro.")
                st.rerun()

with aba_admin:
    senha_configurada = secret_or_env("ADMIN_PASSWORD")
    if not senha_configurada:
        st.info("Painel desativado. Configure ADMIN_PASSWORD nas configuraÃ§Ãµes privadas do aplicativo.")
    else:
        import hmac
        senha = st.text_input("Senha do administrador", type="password")
        if senha and hmac.compare_digest(senha, senha_configurada):
            data_filtro = st.date_input("Data para consultar", value=date.today(), key="admin_data", format="DD/MM/YYYY")
            with conectar() as c:
                registros = c.execute("""SELECT id, horario, nome, telefone, servico, preco, status
                    FROM agendamentos WHERE data=? ORDER BY horario""", (data_filtro.isoformat(),)).fetchall()
            if not registros:
                st.info("Nenhum agendamento para essa data.")
            for ident, hora, pessoa, fone, serv, valor, status in registros:
                with st.container(border=True):
                    st.markdown(f"**{hora} â€” {pessoa}**")
                    st.write(f"{serv} | R$ {valor:.2f} | {status} | WhatsApp: {fone}")
                    if status == "Confirmado" and st.button("Cancelar agendamento", key=f"cancel_{ident}"):
                        cancelar(ident)
                        st.rerun()
        elif senha:
            st.error("Senha incorreta.")

import os, re, time, secrets, sqlite3, hashlib, hmac
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from contextlib import contextmanager
import streamlit as st
import psycopg
from psycopg.rows import dict_row

def setting(key, default=''):
    try:
        return str(st.secrets.get(key, os.environ.get(key, default)))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(key, default)



ROOT = Path(__file__).resolve().parent

DATA = Path(os.environ.get('DATA_DIR', ROOT / 'dados'))

DATA.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo('America/Sao_Paulo')

PIX = '31998908254'

PAYMENTS = ['Pix', 'Dinheiro', 'Cartão de crédito', 'Cartão de débito']

CATALOG = [
    ('Acabamento (pezinho)',1800,15), ('Barba',3000,20), ('Barba e pezinho',4500,20),
    ('Corte barba e textura',8500,75), ('Corte barba e tinta',9000,75), ('Corte',3500,30),
    ('Corte e sobrancelha',4800,40), ('Corte e tinta',6500,50), ('Corte só na tesoura',3800,40),
    ('Textura (relaxamento)',3500,30), ('Tinta',2800,15), ('Corte barba sobrancelha',7000,60),
    ('Corte e barba',6500,60), ('Corte textura',6800,50), ('Corte tinta e sobrancelha',7500,50),
    ('Pezinho e sobrancelha',3000,15), ('Pezinho tinta',4500,35)]

class PostgreSQL:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=()):
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            return self.connection.execute('SELECT pg_advisory_xact_lock(8432752008)')
        return self.connection.execute(sql.replace('?', '%s'), tuple(args))

    def executescript(self, sql):
        sql = sql.replace('id INTEGER PRIMARY KEY', 'id BIGSERIAL PRIMARY KEY')
        sql = sql.replace('expires REAL', 'expires DOUBLE PRECISION')
        for statement in sql.split(';'):
            if statement.strip():
                self.connection.execute(statement)

    def executemany(self, sql, rows):
        with self.connection.cursor() as cursor:
            for row in rows:
                cursor.execute(sql.replace('?', '%s'), row)


@contextmanager
def db():
    url = setting('DATABASE_URL')
    if url:
        c = psycopg.connect(url, row_factory=dict_row, sslmode='require',
                            connect_timeout=15, prepare_threshold=None)
        try:
            # Serializa as reservas concorrentes no banco externo.
            c.execute('SELECT pg_advisory_xact_lock(8432752008)')
            c.execute('CREATE SCHEMA IF NOT EXISTS damasceno')
            c.execute('SET LOCAL search_path TO damasceno')
            yield PostgreSQL(c)
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()
    else:
        if setting('DEMO_MODE', 'false').lower() not in ('1', 'true'):
            raise ValueError('Configure DATABASE_URL nos Secrets do Streamlit.')
        c = sqlite3.connect(DATA / 'barbearia.sqlite3', timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

def initialize():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY,name TEXT NOT NULL,
            price INTEGER NOT NULL,duration INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY,name TEXT NOT NULL,phone TEXT NOT NULL,
            service TEXT NOT NULL,date TEXT NOT NULL,start INTEGER NOT NULL,duration INTEGER NOT NULL,
            price INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'agendado',payment TEXT NOT NULL DEFAULT '',
            cancel_hash TEXT UNIQUE NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auth(token TEXT PRIMARY KEY,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(ip TEXT PRIMARY KEY,count INTEGER NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(id TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        c.execute('BEGIN IMMEDIATE')
        if not c.execute("SELECT 1 FROM settings WHERE id='initialized'").fetchone():
            c.executemany('INSERT INTO services(name,price,duration) VALUES(?,?,?)', CATALOG)
            c.execute("INSERT INTO settings VALUES('initialized','1')")

def now(): return datetime.now(TZ)

def money(n): return 'R$ ' + f'{n/100:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

def clock(m): return f'{m//60:02d}:{m%60:02d}'

def digest(t): return hashlib.sha256(t.encode()).hexdigest()

def selected(ids):
    if not ids or len(ids)>30 or len(set(ids))!=len(ids): raise ValueError('Selecione serviços válidos.')
    with db() as c:
        rows = c.execute('SELECT * FROM services WHERE active=1 AND id IN ('+','.join('?' for _ in ids)+') ORDER BY name',ids).fetchall()
    if len(rows)!=len(ids): raise ValueError('Algum serviço não está mais disponível.')
    return rows

def valid(date, start, duration):
    try:
        day = datetime.strptime(date,'%Y-%m-%d').date()
        current = now()
        return (current.date() <= day <= current.date()+timedelta(days=90)
            and day.weekday()<6 and start%30==0 and start>=480 and start+duration<=1140
            and not(start<780 and start+duration>690)
            and datetime.combine(day,datetime.min.time(),TZ)+timedelta(minutes=start)>current)
    except (ValueError, TypeError): return False

def times(date, duration):
    with db() as c:
        busy = c.execute("SELECT start,duration FROM bookings WHERE date=? AND status!='cancelado'",(date,)).fetchall()
    return [m for m in range(480,1140,30) if valid(date,m,duration)
        and not any(m<b['start']+b['duration'] and m+duration>b['start'] for b in busy)]

def can_cancel(b):
    return b['status']=='agendado' and datetime.fromisoformat(b['date']).replace(tzinfo=TZ)+timedelta(minutes=b['start'])>now()

def cents(value):
    from decimal import Decimal, InvalidOperation
    try:
        n = Decimal(str(value).replace(',','.'))*100
        if not n.is_finite() or n!=n.to_integral_value() or not 0<=n<=1000000: raise ValueError()
        return int(n)
    except (InvalidOperation,ValueError): raise ValueError('Informe um valor válido com até duas casas decimais.')

try:
    initialize()
except (psycopg.Error, ValueError):
    st.error('Configure DATABASE_URL nos Secrets do Streamlit com a conexão Session pooler do Supabase e a senha do banco. Verifique se o projeto está ativo.')
    st.stop()



def create_booking(ids, date, start, name, phone):
    rows = selected([str(i) for i in ids])
    name = name.strip()
    phone = re.sub(r'\D', '', phone)
    duration = sum(s['duration'] for s in rows)
    price = sum(s['price'] for s in rows)
    if not 2 <= len(name) <= 80 or len(phone) not in (10, 11):
        raise ValueError('Confira nome e celular com DDD.')
    if not valid(date, start, duration):
        raise ValueError('Horário indisponível. Atualize os horários.')
    token = secrets.token_hex(32)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM bookings WHERE date=? AND status!='cancelado' AND start<? AND start+duration>?", (date, start+duration, start)).fetchone():
            raise ValueError('Esse horário acaba de ser reservado. Escolha outro.')
        c.execute('INSERT INTO bookings(name,phone,service,date,start,duration,price,cancel_hash,created) VALUES(?,?,?,?,?,?,?,?,?)',
                  (name,phone,' + '.join(s['name'] for s in rows),date,start,duration,price,digest(token),now().isoformat()))
    return token


def read_booking(token):
    if not re.fullmatch('[a-f0-9]{64}', token):
        return None
    with db() as c:
        return c.execute('SELECT * FROM bookings WHERE cancel_hash=?', (digest(token),)).fetchone()


def cancel_booking(token):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        b = c.execute('SELECT * FROM bookings WHERE cancel_hash=?', (digest(token),)).fetchone()
        if not b or not can_cancel(b):
            raise ValueError('O cancelamento só pode ser feito antes do horário, para reservas ainda agendadas.')
        c.execute("UPDATE bookings SET status='cancelado',payment='' WHERE id=?", (b['id'],))


def appointment_ui(token):
    b = read_booking(token)
    if b is None:
        st.error('Link inválido ou agendamento não encontrado.')
        return
    st.subheader('Seu agendamento')
    st.write(b['service'])
    st.write(f"César · {datetime.strptime(b['date'],'%Y-%m-%d').strftime('%d/%m/%Y')} às {clock(b['start'])}")
    st.write(f"{b['duration']} minutos · A partir de {money(b['price'])}")
    st.write('Status: '+b['status'])
    base = setting('APP_URL') or getattr(st.context, 'url', '') or ''
    link = base.rstrip('/')+'?token='+token
    st.caption('Guarde o link particular para consultar e cancelar.')
    st.code(link, language=None)
    message = f"Barbearia Damasceno: {b['service']}, dia {b['date']} às {clock(b['start'])}, com César. Consulte ou cancele: {link}"
    st.link_button('Compartilhar no WhatsApp', 'https://wa.me/?text='+quote(message, safe=''))
    st.caption('A mensagem não foi enviada automaticamente. Compartilhe o link somente com quem deve ter acesso à reserva.')
    if can_cancel(b):
        with st.form('cancelar'):
            confirm = st.checkbox('Quero cancelar este agendamento')
            sent = st.form_submit_button('Cancelar agendamento')
        if sent:
            if not confirm:
                st.error('Marque a confirmação para cancelar.')
            else:
                try:
                    cancel_booking(token)
                    st.session_state['notice']='Agendamento cancelado. O horário foi liberado.'
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    if st.button('Fazer outro agendamento'):
        st.query_params.clear()
        st.rerun()


def booking_ui():
    st.title('Agende com o César.')
    st.write('Escolha um ou mais serviços, o dia e o horário.')
    with db() as c:
        catalog = c.execute('SELECT * FROM services WHERE active=1 ORDER BY name').fetchall()
    options = {s['id']:s for s in catalog}
    ids = st.multiselect('Serviços',list(options),format_func=lambda i: f"{options[i]['name']} · {options[i]['duration']} min · a partir de {money(options[i]['price'])}")
    day = st.date_input('Dia do atendimento',min_value=now().date(),max_value=now().date()+timedelta(days=90))
    st.caption('Segunda a sábado: 08:00–11:30 e 13:00–19:00. Domingo fechado.')
    if not ids:
        st.info('Selecione pelo menos um serviço.')
        return
    chosen = selected([str(i) for i in ids])
    duration = sum(s['duration'] for s in chosen)
    price = sum(s['price'] for s in chosen)
    st.write(f'Tempo total: {duration} minutos · A partir de {money(price)}')
    if st.button('Atualizar horários'):
        st.rerun()
    available = times(day.isoformat(),duration)
    if not available:
        st.info('Sem horários disponíveis para estes serviços nesse dia. Escolha outro dia.')
        return
    start = st.selectbox('Horário disponível',available,format_func=clock)
    with st.form('reservar',clear_on_submit=False):
        name = st.text_input('Seu nome',max_chars=80)
        phone = st.text_input('Celular com DDD',max_chars=20)
        submitted = st.form_submit_button('Confirmar agendamento',type='primary')
    if submitted:
        try:
            token = create_booking(ids,day.isoformat(),start,name,phone)
            st.query_params['token']=token
            st.session_state['notice']='Agendamento confirmado. Guarde seu link.'
            st.rerun()
        except ValueError as e:
            st.error(str(e))


def is_admin():
    return st.session_state.get('auth_until',0)>time.time()


def management_ui():
    st.title('Gestão Damasceno')
    if not is_admin():
        until=st.session_state.get('blocked_until',0)
        if until>time.time():
            st.error('Muitas tentativas. Aguarde 15 minutos.')
            return
        with st.form('login'):
            password=st.text_input('Senha de gestão',type='password')
            sent=st.form_submit_button('Entrar no painel')
        if sent:
            if hmac.compare_digest(digest(password),digest(setting('ADMIN_PASSWORD','1980'))):
                st.session_state['auth_until']=time.time()+43200
                st.session_state['login_attempts']=0
                st.rerun()
            else:
                tries=st.session_state.get('login_attempts',0)+1
                st.session_state['login_attempts']=tries
                if tries>=10:
                    st.session_state['blocked_until']=time.time()+900
                    st.session_state['login_attempts']=0
                st.error('Senha incorreta.')
        return
    if st.button('Sair da gestão'):
        st.session_state.pop('auth_until',None)
        st.rerun()
    agenda,finance,services,clients=st.tabs(['Agenda','Recebimentos','Serviços','Clientes'])
    with agenda:
        day=st.date_input('Dia da agenda',value=now().date(),key='admin_day')
        if st.button('Atualizar agenda'): st.rerun()
        with db() as c:
            rows=c.execute('SELECT * FROM bookings WHERE date=? ORDER BY start',(day.isoformat(),)).fetchall()
        if not rows: st.info('Nenhum agendamento neste dia.')
        for b in rows:
            with st.expander(f"{clock(b['start'])} · {b['name']} · {b['status']}"):
                st.write(b['service'])
                st.write(f"{b['duration']} min · {money(b['price'])} · {b['phone']}")
                st.link_button('Abrir WhatsApp','https://wa.me/55'+b['phone'])
                if b['status']=='agendado':
                    with st.form('status_'+str(b['id'])):
                        amount=st.number_input('Valor recebido (R$)',min_value=0.0,max_value=10000.0,value=b['price']/100,step=0.01)
                        payment=st.selectbox('Forma de pagamento',PAYMENTS)
                        finish=st.form_submit_button('Concluir atendimento')
                        confirm=st.checkbox('Confirmo o cancelamento desta reserva')
                        cancel=st.form_submit_button('Cancelar reserva')
                    if finish or cancel:
                        if cancel and not confirm:
                            st.error('Confirme o cancelamento.')
                        else:
                            with db() as c:
                                result=c.execute("UPDATE bookings SET status=?,payment=?,price=? WHERE id=? AND status='agendado'",('concluido' if finish else 'cancelado',payment if finish else '',cents(str(amount)) if finish else b['price'],b['id']))
                            st.session_state['notice']='Atendimento atualizado.' if result.rowcount else 'A reserva já foi finalizada.'
                            st.rerun()
    with finance:
        month=st.text_input('Mês (AAAA-MM)',value=now().strftime('%Y-%m'))
        if re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month):
            with db() as c:
                rows=c.execute("SELECT date,name,service,price,payment FROM bookings WHERE status='concluido' AND substr(date,1,7)=? ORDER BY date",(month,)).fetchall()
            st.metric('Recebido no mês',money(sum(b['price'] for b in rows)))
            st.dataframe([dict(b)|{'price':money(b['price'])} for b in rows],width='stretch')
        else: st.error('Use o formato AAAA-MM.')
    with services:
        with db() as c: catalog=c.execute('SELECT * FROM services ORDER BY name').fetchall()
        choices={s['id']:s for s in catalog}
        choice=st.selectbox('Serviço para editar',[None]+list(choices),format_func=lambda i:'Cadastrar novo serviço' if i is None else choices[i]['name'])
        old=choices.get(choice)
        with st.form('service'):
            name=st.text_input('Nome do serviço',value=old['name'] if old else '',max_chars=80)
            price=st.number_input('Preço inicial (R$)',min_value=0.0,max_value=10000.0,value=old['price']/100 if old else 0.0,step=0.01)
            duration=st.number_input('Duração (minutos)',min_value=15,max_value=240,value=old['duration'] if old else 30,step=5)
            active=st.checkbox('Disponível para agendamento',value=bool(old['active']) if old else True)
            save=st.form_submit_button('Salvar serviço')
        if save:
            if not name.strip() or duration%5:
                st.error('Informe nome e duração em múltiplos de 5 minutos.')
            else:
                with db() as c:
                    if choice is None:
                        c.execute('INSERT INTO services(name,price,duration,active) VALUES(?,?,?,?)',(name.strip(),cents(str(price)),duration,int(active)))
                    else:
                        c.execute('UPDATE services SET name=?,price=?,duration=?,active=? WHERE id=?',(name.strip(),cents(str(price)),duration,int(active),choice))
                st.session_state['notice']='Serviço salvo.'
                st.rerun()
    with clients:
        with db() as c:
            rows=c.execute('SELECT name AS Nome,phone AS Celular,COUNT(*) AS Reservas FROM bookings GROUP BY phone,name ORDER BY name').fetchall()
        st.dataframe([dict(b) for b in rows],width='stretch')


st.set_page_config(page_title='Barbearia Damasceno',page_icon='💈',layout='centered')
if not setting('DATABASE_URL'):
    st.warning('Modo de demonstração: o banco local não é persistente no Streamlit Cloud.')
if (ROOT/'logo.png').exists(): st.image(str(ROOT/'logo.png'),width=110)
st.caption('DAMASCENO · BARBEARIA · DESDE 2008')
if 'notice' in st.session_state: st.success(st.session_state.pop('notice'))
mode=st.sidebar.radio('Menu',['Agendar','Gestão'])
if mode=='Gestão':
    management_ui()
elif st.query_params.get('token'):
    appointment_ui(str(st.query_params['token']))
else:
    booking_ui()
st.divider()
left,right=st.columns(2)
with left:
    st.subheader('Onde estamos')
    st.write('R. José Mendes Ferreira, 812 — Colorado, Contagem — MG, 32143-000')
    st.write('Segunda a sábado: 08:00 às 11:30 e 13:00 às 19:00. Domingo fechado.')
    st.link_button('(31) 99890-8254','tel:+5531998908254')
with right:
    st.subheader('Formas de pagamento')
    st.write('Pix · Dinheiro · Cartão de crédito · Cartão de débito')
    st.caption('Chave Pix — celular')
    st.code(PIX,language=None)

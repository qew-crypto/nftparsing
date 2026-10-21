import asyncio, html, json, logging, os, re, sqlite3, time
from pathlib import Path
from urllib.parse import urlparse
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

ROOT=Path(__file__).resolve().parent; DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
load_dotenv(ROOT/'.env')
TOKEN=os.getenv('BOT_TOKEN','').strip(); OWNER_CHAT_ID=int(os.getenv('OWNER_CHAT_ID','0'))
SCAN_INTERVAL=max(15,int(os.getenv('SCAN_INTERVAL','60')))
COLLECTION_REFRESH=max(300,int(os.getenv('COLLECTION_REFRESH','3600')))
BACKGROUND_SCAN_DEPTH=max(20,int(os.getenv('BACKGROUND_SCAN_DEPTH','250')))
CONCURRENCY=max(1,min(15,int(os.getenv('CONCURRENCY','6'))))
if not TOKEN or not OWNER_CHAT_ID: raise RuntimeError('Заполните BOT_TOKEN и OWNER_CHAT_ID в .env')
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s'); log=logging.getLogger('giftbot')
DB=sqlite3.connect(DATA/'bot.sqlite3'); DB.row_factory=sqlite3.Row
DB.executescript('''
CREATE TABLE IF NOT EXISTS collections(slug TEXT PRIMARY KEY,name TEXT NOT NULL,last_number INTEGER,updated_at INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS gifts(slug TEXT NOT NULL,number INTEGER NOT NULL,owner TEXT,model TEXT,backdrop TEXT,symbol TEXT,url TEXT,center_color TEXT,edge_color TEXT,pattern_color TEXT,text_color TEXT,PRIMARY KEY(slug,number));
CREATE TABLE IF NOT EXISTS scans(slug TEXT PRIMARY KEY,backgrounds_scanned INTEGER DEFAULT 0);
'''); DB.commit()
HEADERS={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36','Cache-Control':'no-cache'}
FALLBACK_URL='https://raw.githubusercontent.com/Nxrix/Telegram-Gifts/main/data/gifts_names.json'

def slugify(name): return re.sub(r'[^A-Za-z0-9]','',name).lower()
def rows(soup):
 d={}; table=soup.find('table',class_='tgme_gift_table')
 if table:
  for tr in table.find_all('tr'):
   th,td=tr.find('th'),tr.find('td')
   if th and td:d[th.get_text(' ',strip=True).lower()]=td
 return d
def owner(cell):
 if not cell:return 'не указан'
 label=cell.get_text(' ',strip=True); a=cell.find('a',href=True)
 if a:
  p=urlparse(a['href']); u=p.path.strip('/').split('/')[0]
  if p.netloc.lower() in {'t.me','telegram.me','www.t.me'} and u and u.lower()!='nft':return '@'+u
 m=re.fullmatch(r'([A-Za-z0-9_]{5,32})\.t\.me',label); return '@'+m.group(1) if m else label
def svg_colors(soup):
 out=['не указан']*4; svg=soup.select_one('div.tgme_gift_preview > svg')
 if not svg:return out
 stops=svg.find_all('stop')
 if stops:out[0]=stops[0].get('stop-color',out[0])
 if len(stops)>1:out[1]=stops[1].get('stop-color',out[1])
 pat=svg.find(id=re.compile(r'gift.*PatternColor',re.I))
 if pat:out[2]=pat.get('flood-color') or pat.get('fill') or out[2]
 txt=svg.find(id=re.compile(r'gift.*TextColor',re.I))
 if txt:out[3]=txt.get('fill') or txt.get('color') or out[3]
 return out
def parse_gift(body,slug,number):
 soup=BeautifulSoup(body,'html.parser'); r=rows(soup)
 if not r or 'quantity' not in r:return None
 q=r['quantity'].get_text(' ',strip=True); m=re.search(r'([\d\s,\u00a0]+)\s*/',q)
 issued=int(re.sub(r'\D','',m.group(1))) if m else None; c=svg_colors(soup)
 val=lambda k,default:r[k].get_text(' ',strip=True) if k in r else default
 return {'slug':slug,'number':number,'issued':issued,'url':f'https://t.me/nft/{slug}-{number}','owner':owner(r.get('owner')),'model':val('model','не указана'),'backdrop':val('backdrop','не указан'),'symbol':val('symbol','не указан'),'center_color':c[0],'edge_color':c[1],'pattern_color':c[2],'text_color':c[3]}
async def fetch_gift(session,slug,number):
 try:
  async with session.get(f'https://t.me/nft/{slug}-{number}?v={time.time_ns()}') as resp:
   if resp.status in (404,429):return None
   resp.raise_for_status(); return parse_gift(await resp.text(),slug,number)
 except (aiohttp.ClientError,asyncio.TimeoutError):return None
def save_gift(g):
 DB.execute('''INSERT OR REPLACE INTO gifts(slug,number,owner,model,backdrop,symbol,url,center_color,edge_color,pattern_color,text_color) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(g['slug'],g['number'],g['owner'],g['model'],g['backdrop'],g['symbol'],g['url'],g['center_color'],g['edge_color'],g['pattern_color'],g['text_color']));DB.commit()
def gift_text(g,title='🎁 Новый апгрейд!'):
 e=lambda x:html.escape(str(x)); return (f'<b>{title}</b>\n\n🔗 <b>Подарок:</b> <a href="{g["url"]}">{e(g["slug"])} #{g["number"]}</a>\n👤 <b>Владелец:</b> {e(g["owner"])}\n\n🧩 <b>Модель:</b> {e(g["model"])}\n🌌 <b>Фон:</b> {e(g["backdrop"])}\n✨ <b>Символ:</b> {e(g["symbol"])}\n\n<b>🎨 Цвета</b>\n• Центр: <code>{e(g["center_color"])}</code>\n• Край: <code>{e(g["edge_color"])}</code>\n• Узор: <code>{e(g["pattern_color"])}</code>\n• Текст: <code>{e(g["text_color"])}</code>')
async def discover(session):
 found={}
 try:
  async with session.get('https://fragment.com/gifts') as resp:
   soup=BeautifulSoup(await resp.text(),'html.parser')
   for a in soup.find_all('a',href=True):
    m=re.fullmatch(r'/gifts/([a-z0-9-]+)',a['href'].split('?')[0])
    name=a.get_text(' ',strip=True)
    if m and name and name.lower()!='all collections':found[m.group(1)]=re.sub(r'\s+[\d,]+$','',name).strip()
 except Exception:log.exception('Fragment недоступен')
 if not found:
  try:
   async with session.get(FALLBACK_URL) as resp:
    for name in await resp.json():found[slugify(name)]=name
  except Exception:log.exception('Не удалось получить резервный список')
 for slug,name in found.items():DB.execute('INSERT INTO collections(slug,name) VALUES(?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name',(slug,name))
 DB.commit(); log.info('Коллекций: %s',len(found)); return found
async def monitor_one(session,bot,row):
 slug=row['slug']; probe=await fetch_gift(session,slug,1)
 if not probe or not probe['issued']:return
 current=probe['issued']; last=row['last_number']
 if last is None:
  DB.execute('UPDATE collections SET last_number=?,updated_at=? WHERE slug=?',(current,int(time.time()),slug));DB.commit();return
 for n in range(last+1,current+1):
  g=await fetch_gift(session,slug,n)
  if not g:break
  save_gift(g); await bot.send_message(OWNER_CHAT_ID,gift_text(g),parse_mode='HTML',link_preview_options={'is_disabled':True})
  DB.execute('UPDATE collections SET last_number=?,updated_at=? WHERE slug=?',(n,int(time.time()),slug));DB.commit()
async def watcher(bot,session):
 last_refresh=0; sem=asyncio.Semaphore(CONCURRENCY)
 async def run(row):
  async with sem:
   try:await monitor_one(session,bot,row)
   except Exception:log.exception('Ошибка %s',row['slug'])
 while True:
  if time.time()-last_refresh>COLLECTION_REFRESH:await discover(session);last_refresh=time.time()
  rs=DB.execute('SELECT * FROM collections ORDER BY name').fetchall()
  await asyncio.gather(*(run(r) for r in rs)); await asyncio.sleep(SCAN_INTERVAL)
def main_kb():return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎁 Последний улучшенный подарок',callback_data='latest:0')],[InlineKeyboardButton(text='🔄 Обновить коллекции',callback_data='refresh')]])
def collections_kb(page=0):
 per=8; rs=DB.execute('SELECT slug,name,last_number FROM collections ORDER BY name').fetchall(); pages=max(1,(len(rs)+per-1)//per);page=max(0,min(page,pages-1));part=rs[page*per:(page+1)*per]
 keys=[[InlineKeyboardButton(text=f'{r["name"]} · #{r["last_number"] or "…"}',callback_data=f'col:{r["slug"]}:{page}')] for r in part]
 nav=[]
 if page:nav.append(InlineKeyboardButton(text='⬅️',callback_data=f'latest:{page-1}'))
 nav.append(InlineKeyboardButton(text=f'{page+1}/{pages}',callback_data='noop'))
 if page+1<pages:nav.append(InlineKeyboardButton(text='➡️',callback_data=f'latest:{page+1}'))
 keys+=[nav,[InlineKeyboardButton(text='🏠 Меню',callback_data='menu')]];return InlineKeyboardMarkup(inline_keyboard=keys)
def backgrounds(slug):return [r['backdrop'] for r in DB.execute("SELECT backdrop,MAX(number) n FROM gifts WHERE slug=? AND backdrop IS NOT NULL AND backdrop!='не указан' GROUP BY backdrop ORDER BY backdrop",(slug,)).fetchall()]
def backgrounds_kb(slug,page=0):
 vals=backgrounds(slug);per=8;pages=max(1,(len(vals)+per-1)//per);page=max(0,min(page,pages-1));keys=[[InlineKeyboardButton(text='🌈 Любой фон',callback_data=f'any:{slug}')]] if page==0 else []
 for i,name in enumerate(vals[page*per:(page+1)*per],start=page*per):keys.append([InlineKeyboardButton(text=name[:50],callback_data=f'bg:{slug}:{i}')])
 nav=[]
 if page:nav.append(InlineKeyboardButton(text='⬅️',callback_data=f'bgp:{slug}:{page-1}'))
 nav.append(InlineKeyboardButton(text=f'{page+1}/{pages}',callback_data='noop'))
 if page+1<pages:nav.append(InlineKeyboardButton(text='➡️',callback_data=f'bgp:{slug}:{page+1}'))
 keys+=[nav,[InlineKeyboardButton(text='⬅️ Коллекции',callback_data='latest:0')]];return InlineKeyboardMarkup(inline_keyboard=keys)
async def backfill(session,slug,last):
 done=DB.execute('SELECT backgrounds_scanned FROM scans WHERE slug=?',(slug,)).fetchone()
 if done and done[0]:return
 sem=asyncio.Semaphore(CONCURRENCY)
 async def one(n):
  async with sem:
   g=await fetch_gift(session,slug,n)
   if g:save_gift(g)
 start=max(1,last-BACKGROUND_SCAN_DEPTH+1);await asyncio.gather(*(one(n) for n in range(start,last+1)))
 DB.execute('INSERT INTO scans(slug,backgrounds_scanned) VALUES(?,1) ON CONFLICT(slug) DO UPDATE SET backgrounds_scanned=1',(slug,));DB.commit()
async def main():
 bot=Bot(TOKEN);dp=Dispatcher();timeout=aiohttp.ClientTimeout(total=20);session=aiohttp.ClientSession(headers=HEADERS,timeout=timeout)
 @dp.message(CommandStart())
 async def start(m:Message):
  if m.chat.id!=OWNER_CHAT_ID:return
  await m.answer('Бот отслеживает новые номера во всех найденных NFT-коллекциях. Выбери действие:',reply_markup=main_kb())
 @dp.callback_query(F.data=='menu')
 async def menu(c:CallbackQuery):await c.message.edit_text('Главное меню:',reply_markup=main_kb());await c.answer()
 @dp.callback_query(F.data.startswith('latest:'))
 async def latest(c:CallbackQuery):
  page=int(c.data.split(':')[1]);await c.message.edit_text('Выбери NFT-коллекцию:',reply_markup=collections_kb(page));await c.answer()
 @dp.callback_query(F.data=='refresh')
 async def refresh(c:CallbackQuery):await c.answer('Обновляю список…');await discover(session);await c.message.edit_text('Коллекции обновлены.',reply_markup=main_kb())
 @dp.callback_query(F.data.startswith('col:'))
 async def col(c:CallbackQuery):
  _,slug,_=c.data.split(':');r=DB.execute('SELECT name,last_number FROM collections WHERE slug=?',(slug,)).fetchone()
  if not r or not r['last_number']:await c.answer('Последний номер ещё определяется',show_alert=True);return
  await c.answer('Собираю последние фоны…');await backfill(session,slug,r['last_number']);await c.message.edit_text(f'<b>{html.escape(r["name"])}</b>\nПоследний номер: <b>#{r["last_number"]}</b>\n\nВыбери фон:',parse_mode='HTML',reply_markup=backgrounds_kb(slug,0))
 @dp.callback_query(F.data.startswith('bgp:'))
 async def bgp(c:CallbackQuery):
  _,slug,page=c.data.split(':');await c.message.edit_reply_markup(reply_markup=backgrounds_kb(slug,int(page)));await c.answer()
 @dp.callback_query(F.data.startswith('any:'))
 async def any_bg(c:CallbackQuery):
  slug=c.data.split(':',1)[1];g=DB.execute('SELECT * FROM gifts WHERE slug=? ORDER BY number DESC LIMIT 1',(slug,)).fetchone()
  if not g:await c.answer('Данные ещё собираются',show_alert=True);return
  await c.message.edit_text(gift_text(g,'🎁 Последний подарок'),parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ К фонам',callback_data=f'bgp:{slug}:0')]]),link_preview_options={'is_disabled':True});await c.answer()
 @dp.callback_query(F.data.startswith('bg:'))
 async def bg(c:CallbackQuery):
  _,slug,idx=c.data.split(':');vals=backgrounds(slug);i=int(idx)
  if i>=len(vals):await c.answer('Список обновился, открой заново',show_alert=True);return
  g=DB.execute('SELECT * FROM gifts WHERE slug=? AND backdrop=? ORDER BY number DESC LIMIT 1',(slug,vals[i])).fetchone()
  await c.message.edit_text(gift_text(g,'🎁 Последний подарок с этим фоном'),parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ К фонам',callback_data=f'bgp:{slug}:0')]]),link_preview_options={'is_disabled':True});await c.answer()
 @dp.callback_query(F.data=='noop')
 async def noop(c:CallbackQuery):await c.answer()
 await discover(session);task=asyncio.create_task(watcher(bot,session))
 try:await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
 finally:task.cancel();await session.close();await bot.session.close();DB.close()
if __name__=='__main__':asyncio.run(main())

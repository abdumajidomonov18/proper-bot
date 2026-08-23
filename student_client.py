import requests
import json
import logging
import copy
from bs4 import BeautifulSoup
import re

BASE_URL = "https://proper.lc-up.com"

class StudentClient:
    def __init__(self, phone, password):
        self.phone = phone
        self.password = password
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json, text/html, application/xhtml+xml',
            'Content-Type': 'application/json',
            'X-Livewire': 'true',
            'Referer': f"{BASE_URL}/login"
        }
        self.initial_data = None
        self.is_logged_in = False
        self.dash_html = None
        self.shop_initial_data = None
        self._shop_products_cache = None
        self.selected_student_id = None

    def _get_csrf(self, html_text):
        soup = BeautifulSoup(html_text, 'html.parser')
        tag = soup.find('input', {'name': '_token'})
        return tag['value'] if tag else None

    def _do_post(self, updates):
        url = f"{BASE_URL}/livewire/message/login-livewire"
        payload = {
            'fingerprint': self.initial_data['fingerprint'],
            'serverMemo': self.initial_data['serverMemo'],
            'updates': updates
        }
        resp = self.session.post(url, json=payload, headers=self.headers)
        if resp.status_code == 200:
            j = resp.json()
            sm = j.get('serverMemo', {})
            if 'data' in sm: self.initial_data['serverMemo']['data'].update(sm['data'])
            if 'errors' in sm: self.initial_data['serverMemo']['errors'] = sm['errors']
            if 'checksum' in sm: self.initial_data['serverMemo']['checksum'] = sm['checksum']
            if 'htmlHash' in sm: self.initial_data['serverMemo']['htmlHash'] = sm['htmlHash']
            if 'dataMeta' in sm:
                if 'dataMeta' not in self.initial_data['serverMemo']:
                    self.initial_data['serverMemo']['dataMeta'] = {}
                self.initial_data['serverMemo']['dataMeta'].update(sm['dataMeta'])
            return j
        return None

    def _get_page(self, url):
        resp = self.session.get(url, headers=self.headers)
        if "login" in resp.url.lower() and "/login" not in url:
            logging.info(f"Page {url} redirected to login. Re-authenticating...")
            res = self.login()
            if res.get('status') == 'SUCCESS' or res.get('status') == 'NEEDS_SELECTION':
                if hasattr(self, 'selected_student_id') and self.selected_student_id:
                    self.select_student(self.selected_student_id)
                resp = self.session.get(url, headers=self.headers)
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        token_tag = soup.find('input', {'name': '_token'})
        if token_tag and token_tag.get('value'):
            self.headers['X-CSRF-TOKEN'] = token_tag['value']
            
        return resp

    def login(self):
        resp = self.session.get(f"{BASE_URL}/login", headers=self.headers)
        csrf = self._get_csrf(resp.text)
        if csrf: self.headers['X-CSRF-TOKEN'] = csrf

        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup.find_all(attrs={'wire:initial-data': True}):
            data = json.loads(tag['wire:initial-data'])
            if data.get('fingerprint', {}).get('name') == 'login-livewire':
                self.initial_data = data
                break
                
        if not self.initial_data: return {'status': 'ERROR'}

        j1 = self._do_post([
            {'type': 'syncInput', 'payload': {'id': 's1', 'name': 'mobile_number', 'value': self.phone}},
            {'type': 'syncInput', 'payload': {'id': 's2', 'name': 'password', 'value': self.password}},
            {'type': 'callMethod', 'payload': {'id': 'c1', 'method': 'save', 'params': []}}
        ])
        if not j1: return {'status': 'ERROR'}
        
        redirect = j1.get('effects', {}).get('redirect')
        if redirect:
            self.headers.pop('X-Livewire', None)
            self.headers.pop('Content-Type', None)
            dash = self.session.get(redirect, headers=self.headers)
            self.dash_html = dash.text
            self.is_logged_in = True
            return {'status': 'SUCCESS'}
        
        students_html = j1.get('effects', {}).get('html', '')
        if students_html and '<option' in students_html:
            # Parse options
            soup_opt = BeautifulSoup(students_html, 'html.parser')
            students_list = []
            for opt in soup_opt.find_all('option'):
                if opt.get('value'):
                    students_list.append({'id': opt['value'], 'name': opt.text.strip()})
            if students_list:
                return {'status': 'NEEDS_SELECTION', 'students': students_list}

        students = self.initial_data['serverMemo'].get('dataMeta', {}).get('modelCollections', {}).get('show_students', {}).get('id', [])
        if not students: return {'status': 'ERROR'}
            
        return self.select_student(str(students[0]))
        
    def select_student(self, student_id):
        self.selected_student_id = student_id
        j2 = self._do_post([
            {'type': 'syncInput', 'payload': {'id': 's3', 'name': 'current_student', 'value': str(student_id)}},
            {'type': 'callMethod', 'payload': {'id': 'c2', 'method': 'save', 'params': []}}
        ])
        
        redirect = j2.get('effects', {}).get('redirect') if j2 else None
        if redirect:
            self.headers.pop('X-Livewire', None)
            self.headers.pop('Content-Type', None)
            dash = self.session.get(redirect, headers=self.headers)
            self.dash_html = dash.text
            self.is_logged_in = True
            return {'status': 'SUCCESS'}
        return {'status': 'ERROR'}
    
    def get_coins(self):
        if not self.dash_html: return "0"
        soup = BeautifulSoup(self.dash_html, 'html.parser')
        for p in soup.find_all('p'):
            if 'tanga' in p.text.lower():
                h3 = p.find_next('h3')
                if h3:
                    return h3.text.strip().replace('\xa0', ' ').replace('\n', ' ')
        return "0"

    def get_crystals(self):
        if not self.dash_html: return "0"
        soup = BeautifulSoup(self.dash_html, 'html.parser')
        for p in soup.find_all('p'):
            if 'kristal' in p.text.lower():
                h3 = p.find_next('h3')
                if h3:
                    return h3.text.strip().replace('\xa0', ' ').replace('\n', ' ')
        return "0"

    def get_books(self):
        resp = self._get_page(f"{BASE_URL}/student/study")
        soup = BeautifulSoup(resp.text, 'html.parser')
        books = []
        for h3 in soup.find_all('h3'):
            title = h3.text.strip()
            a_tag = h3.find_next('a', href=re.compile(r'/student/study/(\d+)/lessons'))
            if a_tag:
                m = re.search(r'/student/study/(\d+)/lessons', a_tag['href'])
                if m: books.append({'id': m.group(1), 'title': title})
        return books
        
    def get_units(self, book_id):
        resp = self._get_page(f"{BASE_URL}/student/study/{book_id}/lessons")
        soup = BeautifulSoup(resp.text, 'html.parser')
        units = []
        for div in soup.find_all(onclick=True):
            if 'openExercisesDiolog' in div['onclick']:
                raw = div.text.strip().replace('\n', ' ')
                clean = re.sub(r'\s+', ' ', raw)
                m = re.search(r'openExercisesDiolog\((\d+)\)', div['onclick'])
                if m: units.append({'id': m.group(1), 'name': clean})
        return units

    def _init_shop(self):
        if self.shop_initial_data and self._shop_products_cache:
            return
            
        resp = self._get_page(f"{BASE_URL}/student/shop")
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        for tag in soup.find_all(attrs={'wire:initial-data': True}):
            data = json.loads(tag['wire:initial-data'])
            if 'student-shop-livewire' in data.get('fingerprint', {}).get('name', ''):
                self.shop_initial_data = data
                break
                
        products = []
        for tag in soup.find_all(attrs={'wire:click': True}):
            click = tag['wire:click']
            if 'showProductDetail' in click:
                m = re.search(r'showProductDetail\((\d+)\)', click)
                if m:
                    pid = int(m.group(1))
                    name = tag.find('h3').text.strip() if tag.find('h3') else "Noma'lum"
                    text = tag.text
                    price_match = re.findall(r'(\d[\d\s]*)\s*(tanga|so\'m)', text)
                    p_tanga = "Noma'lum"
                    p_som = "Noma'lum"
                    for val, unit in price_match:
                        val = val.replace('\xa0', '').replace(' ', '').strip()
                        if unit == 'tanga':
                            p_tanga = val
                        elif unit == "so'm":
                            p_som = val
                    
                    img = tag.find('img')
                    img_url = img['src'] if img and img.get('src') else ""
                    
                    products.append({
                        'id': pid,
                        'name': name,
                        'tanga': p_tanga,
                        'som': p_som,
                        'img_url': img_url
                    })
        self._shop_products_cache = products

    def get_shop_products(self):
        self._init_shop()
        return self._shop_products_cache

    def get_product_detail(self, product_id, retry=True):
        self._init_shop()
        if not self.shop_initial_data:
            logging.warning("DEBUG get_product_detail: shop_initial_data is None!")
            return None
        
        initial = copy.deepcopy(self.shop_initial_data)
            
        h = self.headers.copy()
        h['X-Livewire'] = 'true'
        h['Content-Type'] = 'application/json'
        
        r2 = self.session.post(f"{BASE_URL}/livewire/message/students.student-shop-livewire", json={
            'fingerprint': initial['fingerprint'],
            'serverMemo': initial['serverMemo'],
            'updates': [{'type': 'callMethod', 'payload': {'id': 'det', 'method': 'showProductDetail', 'params': [product_id]}}]
        }, headers=h)
        
        if r2.status_code != 200:
            logging.warning(f"DEBUG get_product_detail: POST returned status {r2.status_code}")
            if retry:
                logging.warning("DEBUG get_product_detail: Retrying with cleared cache...")
                self.shop_initial_data = None
                self._shop_products_cache = None
                return self.get_product_detail(product_id, retry=False)
            return None
            
        j = r2.json()
        eff = j.get('effects', {}) or {}
        red = eff.get('redirect')
        if red and "login" in red.lower():
            if retry:
                logging.warning("DEBUG get_product_detail: Redirected to login inside effects. Re-authenticating...")
                self.shop_initial_data = None
                self._shop_products_cache = None
                res = self.login()
                if res.get('status') == 'SUCCESS' or res.get('status') == 'NEEDS_SELECTION':
                    if hasattr(self, 'selected_student_id') and self.selected_student_id:
                        self.select_student(self.selected_student_id)
                return self.get_product_detail(product_id, retry=False)
            return None
            
        sm = j.get('serverMemo', {})
        if 'data' in sm: initial['serverMemo']['data'].update(sm['data'])
        if 'errors' in sm: initial['serverMemo']['errors'] = sm['errors']
        if 'checksum' in sm: initial['serverMemo']['checksum'] = sm['checksum']
        if 'htmlHash' in sm: initial['serverMemo']['htmlHash'] = sm['htmlHash']
        if 'dataMeta' in sm:
            if 'dataMeta' not in initial['serverMemo']:
                initial['serverMemo']['dataMeta'] = {}
            initial['serverMemo']['dataMeta'].update(sm['dataMeta'])
            
        html = eff.get('html', '') or ''
        soup2 = BeautifulSoup(html, 'html.parser')
        
        modal = soup2.find('div', class_=lambda x: x and 'fixed' in x and 'z-' in x)
        if not modal:
            logging.warning(f"DEBUG get_product_detail: modal div not found! HTML length: {len(html)}")
            logging.warning(f"DEBUG JSON response: {json.dumps(j)}")
            return None
            
        name = "Noma'lum"
        h2 = modal.find('h2', class_=lambda x: x and 'font-bold' in x)
        if h2:
            name = h2.text.strip()
            
        tanga = "0"
        som = "0"
        remaining = "0"
        img_url = ""
        
        img = modal.find('img')
        if img and img.get('src'):
            img_url = img['src']
            
        text_content = ' '.join(modal.text.split())
        tanga_match = re.search(r'(\d[\d\s]*)\s*tanga', text_content)
        if tanga_match:
            tanga = tanga_match.group(1).replace(' ', '')
            
        som_match = re.search(r'(\d[\d\s]*)\s*so\'m', text_content)
        if som_match:
            som = som_match.group(1).replace(' ', '')
            
        rem_match = re.search(r'(\d[\d\s]*)\s*ta qolgan', text_content)
        if rem_match:
            remaining = rem_match.group(1).replace(' ', '')
            
        student_coins = j.get('serverMemo', {}).get('data', {}).get('studentCoins', 0)
        
        return {
            'id': product_id,
            'name': name,
            'tanga': tanga,
            'som': som,
            'remaining': remaining,
            'img_url': img_url,
            'student_coins': student_coins
        }

    def order_product(self, product_id, method='coins', retry=True):
        self._init_shop()
        if not self.shop_initial_data:
            return {'status': 3, 'message': 'Tizim xatoligi (initial data topilmadi).'}
        
        initial = copy.deepcopy(self.shop_initial_data)
            
        h = self.headers.copy()
        h['X-Livewire'] = 'true'
        h['Content-Type'] = 'application/json'
        
        def update_memo(sm):
            if 'data' in sm: initial['serverMemo']['data'].update(sm['data'])
            if 'errors' in sm: initial['serverMemo']['errors'] = sm['errors']
            if 'checksum' in sm: initial['serverMemo']['checksum'] = sm['checksum']
            if 'htmlHash' in sm: initial['serverMemo']['htmlHash'] = sm['htmlHash']
            if 'dataMeta' in sm:
                if 'dataMeta' not in initial['serverMemo']:
                    initial['serverMemo']['dataMeta'] = {}
                initial['serverMemo']['dataMeta'].update(sm['dataMeta'])
                
        r2 = self.session.post(f"{BASE_URL}/livewire/message/students.student-shop-livewire", json={
            'fingerprint': initial['fingerprint'],
            'serverMemo': initial['serverMemo'],
            'updates': [{'type': 'callMethod', 'payload': {'id': 'det', 'method': 'showProductDetail', 'params': [product_id]}}]
        }, headers=h)
        
        if r2.status_code != 200:
            if retry:
                self.shop_initial_data = None
                self._shop_products_cache = None
                return self.order_product(product_id, method, retry=False)
            return {'status': 3, 'message': 'Tizim xatoligi (batafsil ko\'rishda xato).'}
            
        j2 = r2.json()
        eff2 = j2.get('effects', {}) or {}
        red2 = eff2.get('redirect')
        if red2 and "login" in red2.lower():
            if retry:
                self.shop_initial_data = None
                self._shop_products_cache = None
                res = self.login()
                if res.get('status') == 'SUCCESS' or res.get('status') == 'NEEDS_SELECTION':
                    if hasattr(self, 'selected_student_id') and self.selected_student_id:
                        self.select_student(self.selected_student_id)
                return self.order_product(product_id, method, retry=False)
            return {'status': 3, 'message': 'Seans eskirgan.'}
            
        update_memo(j2.get('serverMemo', {}))
        
        r3 = self.session.post(f"{BASE_URL}/livewire/message/students.student-shop-livewire", json={
            'fingerprint': initial['fingerprint'],
            'serverMemo': initial['serverMemo'],
            'updates': [{'type': 'callMethod', 'payload': {'id': 'ord', 'method': 'orderProduct', 'params': [product_id, method]}}]
        }, headers=h)
        
        if r3.status_code != 200:
            return {'status': 3, 'message': 'Tizim xatoligi (buyurtma berishda xato).'}
            
        j = r3.json()
        eff = j.get('effects', {}) or {}
        red = eff.get('redirect')
        if red and "login" in red.lower():
            return {'status': 3, 'message': 'Seans eskirgan.'}
            
        update_memo(j.get('serverMemo', {}))
        
        self._shop_products_cache = None
        self.shop_initial_data = None
        
        dispatches = eff.get('dispatches', []) or []
        for d in dispatches:
            if d.get('event') == 'open-alert':
                data = d.get('data', {})
                return {
                    'status': data.get('status', 3),
                    'message': data.get('message', 'Noma\'lum xatolik.')
                }
                
        return {'status': 3, 'message': 'Platforma javob bermadi.'}

    def get_purchase_history(self, retry=True):
        self._init_shop()
        if not self.shop_initial_data:
            return []
        
        initial = copy.deepcopy(self.shop_initial_data)
            
        h = self.headers.copy()
        h['X-Livewire'] = 'true'
        h['Content-Type'] = 'application/json'
        
        r2 = self.session.post(f"{BASE_URL}/livewire/message/students.student-shop-livewire", json={
            'fingerprint': initial['fingerprint'],
            'serverMemo': initial['serverMemo'],
            'updates': [{'type': 'callMethod', 'payload': {'id': 'hist', 'method': 'switchView', 'params': ['history']}}]
        }, headers=h)
        
        if r2.status_code != 200:
            if retry:
                self.shop_initial_data = None
                self._shop_products_cache = None
                return self.get_purchase_history(retry=False)
            return []
            
        j = r2.json()
        eff = j.get('effects', {}) or {}
        red = eff.get('redirect')
        if red and "login" in red.lower():
            if retry:
                self.shop_initial_data = None
                self._shop_products_cache = None
                res = self.login()
                if res.get('status') == 'SUCCESS' or res.get('status') == 'NEEDS_SELECTION':
                    if hasattr(self, 'selected_student_id') and self.selected_student_id:
                        self.select_student(self.selected_student_id)
                return self.get_purchase_history(retry=False)
            return []
            
        sm = j.get('serverMemo', {})
        if 'data' in sm: initial['serverMemo']['data'].update(sm['data'])
        if 'errors' in sm: initial['serverMemo']['errors'] = sm['errors']
        if 'checksum' in sm: initial['serverMemo']['checksum'] = sm['checksum']
        if 'htmlHash' in sm: initial['serverMemo']['htmlHash'] = sm['htmlHash']
        if 'dataMeta' in sm:
            if 'dataMeta' not in initial['serverMemo']:
                initial['serverMemo']['dataMeta'] = {}
            initial['serverMemo']['dataMeta'].update(sm['dataMeta'])
            
        html = eff.get('html', '') or ''
        soup2 = BeautifulSoup(html, 'html.parser')
        
        orders = []
        for div in soup2.find_all('div', class_=lambda x: x and 'flex' in x and 'md:flex-row' in x):
            img = div.find('img')
            text_content = ' '.join(div.text.split())
            if img and 'tanga' in text_content:
                name = "Noma'lum"
                h3 = div.find('h3')
                if h3:
                    name = h3.text.strip()
                    
                tanga = "0"
                tanga_match = re.search(r'(\d[\d\s]*)\s*tanga', text_content)
                if tanga_match:
                    tanga = tanga_match.group(1).replace(' ', '')
                    
                date_div = div.find('div', class_=lambda x: x and 'text-gray-500' in x)
                date = date_div.text.strip() if date_div else ""
                
                status = "Noma'lum"
                status_span = div.find('span', class_=lambda x: x and 'rounded-full' in x)
                if status_span:
                    status = status_span.text.strip()
                    
                cancel_id = None
                cancel_btn = div.find('button', attrs={'wire:click': True})
                if cancel_btn:
                    click = cancel_btn['wire:click']
                    m = re.search(r'cancelOrder\((\d+)\)', click)
                    if m:
                        cancel_id = int(m.group(1))
                        
                orders.append({
                    'name': name,
                    'tanga': tanga,
                    'date': date,
                    'status': status,
                    'cancel_id': cancel_id,
                    'img_url': img['src'] if img else ""
                })
        return orders

    def cancel_order(self, order_id, retry=True):
        self._init_shop()
        if not self.shop_initial_data:
            return {'status': 3, 'message': 'Tizim xatoligi (initial data topilmadi).'}
        
        initial = copy.deepcopy(self.shop_initial_data)
            
        h = self.headers.copy()
        h['X-Livewire'] = 'true'
        h['Content-Type'] = 'application/json'
        
        def update_memo(sm):
            if 'data' in sm: initial['serverMemo']['data'].update(sm['data'])
            if 'errors' in sm: initial['serverMemo']['errors'] = sm['errors']
            if 'checksum' in sm: initial['serverMemo']['checksum'] = sm['checksum']
            if 'htmlHash' in sm: initial['serverMemo']['htmlHash'] = sm['htmlHash']
            if 'dataMeta' in sm:
                if 'dataMeta' not in initial['serverMemo']:
                    initial['serverMemo']['dataMeta'] = {}
                initial['serverMemo']['dataMeta'].update(sm['dataMeta'])
                
        r2 = self.session.post(f"{BASE_URL}/livewire/message/students.student-shop-livewire", json={
            'fingerprint': initial['fingerprint'],
            'serverMemo': initial['serverMemo'],
            'updates': [{'type': 'callMethod', 'payload': {'id': 'hist', 'method': 'switchView', 'params': ['history']}}]
        }, headers=h)
        
        if r2.status_code != 200:
            if retry:
                self.shop_initial_data = None
                self._shop_products_cache = None
                return self.cancel_order(order_id, retry=False)
            return {'status': 3, 'message': 'Tizim xatoligi.'}
            
        j2 = r2.json()
        eff2 = j2.get('effects', {}) or {}
        red2 = eff2.get('redirect')
        if red2 and "login" in red2.lower():
            if retry:
                self.shop_initial_data = None
                self._shop_products_cache = None
                res = self.login()
                if res.get('status') == 'SUCCESS' or res.get('status') == 'NEEDS_SELECTION':
                    if hasattr(self, 'selected_student_id') and self.selected_student_id:
                        self.select_student(self.selected_student_id)
                return self.cancel_order(order_id, retry=False)
            return {'status': 3, 'message': 'Seans eskirgan.'}
            
        update_memo(j2.get('serverMemo', {}))
        
        r3 = self.session.post(f"{BASE_URL}/livewire/message/students.student-shop-livewire", json={
            'fingerprint': initial['fingerprint'],
            'serverMemo': initial['serverMemo'],
            'updates': [{'type': 'callMethod', 'payload': {'id': 'can', 'method': 'cancelOrder', 'params': [order_id]}}]
        }, headers=h)
        
        if r3.status_code != 200:
            return {'status': 3, 'message': 'Bekor qilishda xatolik yuz berdi.'}
            
        j = r3.json()
        eff = j.get('effects', {}) or {}
        red = eff.get('redirect')
        if red and "login" in red.lower():
            return {'status': 3, 'message': 'Seans eskirgan.'}
            
        update_memo(j.get('serverMemo', {}))
        
        self._shop_products_cache = None
        self.shop_initial_data = None
        
        dispatches = eff.get('dispatches', []) or []
        for d in dispatches:
            if d.get('event') == 'open-alert':
                data = d.get('data', {})
                return {
                    'status': data.get('status', 3),
                    'message': data.get('message', 'Noma\'lum xatolik.')
                }
                
        return {'status': 3, 'message': 'Platforma bekor qilmadi.'}

    def get_referral_data(self):
        resp = self._get_page(f"{BASE_URL}/student/referral")
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup.find_all(attrs={'wire:initial-data': True}):
            try:
                data = json.loads(tag['wire:initial-data'])
                if data.get('fingerprint', {}).get('name') == 'student.referral-livewire':
                    referral_url = data.get('serverMemo', {}).get('data', {}).get('referralUrl', '')
                    referrals = data.get('serverMemo', {}).get('data', {}).get('referrals', [])
                    return {
                        'referral_url': referral_url,
                        'referrals': referrals
                    }
            except Exception as e:
                logging.error(f"Error parsing referral wire data: {e}")
        return None

    def get_groups(self):
        resp = self._get_page(f"{BASE_URL}/student/groups?my-groups")
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        status_map = {
            '3': 'Aktiv',
            '1': 'Sinov',
            '5': 'Muzlatilgan',
            '2': "O'chirilgan"
        }

        groups = []
        for card in soup.find_all('div', class_=lambda x: x and 'group-card' in x):
            status_id = card.get('data-status', '')
            status_label = status_map.get(status_id, "Noma'lum")
            
            h3 = card.find('h3')
            name = h3.text.strip() if h3 else "Noma'lum"
            
            level_p = card.find('p', class_=lambda x: x and 'text-gray-400' in x)
            level = level_p.text.strip() if level_p else ''
            
            days = ''
            time = ''
            paragraphs = card.find_all('p', class_=lambda x: x and ('font-bold' in x or 'font-black' in x))
            for p in paragraphs:
                t = p.text.strip()
                if any(d in t for d in ['Dushanba', 'Seshanba', 'Chorshanba', 'Payshanba', 'Juma', 'Shanba', 'Yakshanba']):
                    days = t
                elif ':' in t and '-' in t:
                    time = t
                    
            btn = card.find('button', onclick=lambda x: x and 'openGroupModal' in x)
            gid = None
            if btn and btn.get('onclick'):
                m = re.search(r'openGroupModal\((\d+)\)', btn['onclick'])
                if m:
                    gid = m.group(1)
                    
            groups.append({
                'id': gid,
                'name': name,
                'level': level,
                'status': status_label,
                'days': days,
                'time': time
            })
        return groups

    def get_group_detail(self, group_id):
        resp = self._get_page(f"{BASE_URL}/student/groups?my-groups")
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        initial_data = None
        for tag in soup.find_all(attrs={'wire:initial-data': True}):
            try:
                d = json.loads(tag['wire:initial-data'])
                if d.get('fingerprint', {}).get('name') == 'student-web.student-groups-detail-livewire':
                    initial_data = d
                    break
            except Exception:
                continue
                
        if not initial_data:
            return None
            
        h = self.headers.copy()
        h['X-Livewire'] = 'true'
        h['Content-Type'] = 'application/json'
        
        payload = {
            'fingerprint': initial_data['fingerprint'],
            'serverMemo': initial_data['serverMemo'],
            'updates': [{'type': 'fireEvent', 'payload': {'id': 'init', 'event': 'initGroupDetails', 'params': [int(group_id)]}}]
        }
        
        r = self.session.post(f"{BASE_URL}/livewire/message/student-web.student-groups-detail-livewire", json=payload, headers=h)
        if r.status_code != 200:
            return None
            
        j = r.json()
        html = j.get('effects', {}).get('html', '') or ''
        soup2 = BeautifulSoup(html, 'html.parser')
        
        title_tag = soup2.find('h2', id='modal-title')
        name = title_tag.text.strip() if title_tag else "Noma'lum"
        
        teacher_tag = soup2.find('p', string=re.compile(r'Guruh ustozi', re.I))
        teacher = teacher_tag.find_next('h4').text.strip() if teacher_tag else "Noma'lum"
        
        price_tag = soup2.find('p', string=re.compile(r'Kurs narxi', re.I))
        price = price_tag.find_next('h4').text.strip() if price_tag else "Noma'lum"
        
        days_tag = soup2.find('p', string=re.compile(r'Dars kunlari', re.I))
        days = days_tag.find_next('p').text.strip() if days_tag else "Noma'lum"
        
        time_tag = soup2.find('p', string=re.compile(r'Dars vaqti', re.I))
        raw_time = time_tag.find_next('p').text if time_tag else "Noma'lum"
        time = ' '.join(raw_time.split())
        
        return {
            'id': group_id,
            'name': name,
            'teacher': teacher,
            'price': price,
            'days': days,
            'time': time
        }

    def get_coins_history(self, page=1):
        resp = self._get_page(f"{BASE_URL}/student/coins-history")
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        initial_data = None
        for tag in soup.find_all(attrs={'wire:initial-data': True}):
            try:
                d = json.loads(tag['wire:initial-data'])
                if d.get('fingerprint', {}).get('name') == 'student.coins-history-livewire':
                    initial_data = d
                    break
            except Exception:
                pass
                
        target_soup = soup
        if page > 1 and initial_data:
            h = self.headers.copy()
            h['X-Livewire'] = 'true'
            h['Content-Type'] = 'application/json'
            
            payload = {
                'fingerprint': initial_data['fingerprint'],
                'serverMemo': initial_data['serverMemo'],
                'updates': [{'type': 'callMethod', 'payload': {'id': 'pg', 'method': 'gotoPage', 'params': [int(page)]}}]
            }
            r2 = self.session.post(f"{BASE_URL}/livewire/message/student.coins-history-livewire", json=payload, headers=h)
            if r2.status_code == 200:
                j2 = r2.json()
                html2 = j2.get('effects', {}).get('html', '') or ''
                target_soup = BeautifulSoup(html2, 'html.parser')
                
        # Parse total pages from nav
        nav = target_soup.find('nav', attrs={'role': 'navigation'}) or target_soup.find('nav')
        total_pages = 1
        if nav:
            nums = [int(x) for x in re.findall(r'\b\d+\b', nav.text)]
            if nums:
                total_pages = max(nums)
                
        history = []
        for section in target_soup.find_all('div', class_=lambda x: x and 'space-y-8' in x):
            for date_block in section.find_all('div', recursive=False):
                h4 = date_block.find('h4')
                if not h4:
                    continue
                date_str = h4.text.strip().replace(' 2026', '')
                
                items = []
                for item_div in date_block.find_all('div', class_=lambda x: x and 'rounded-[24px]' in x and 'flex' in x):
                    img = item_div.find('img')
                    img_src = img['src'] if img and img.get('src') else ''
                    
                    is_crystal = 'diamond' in img_src
                    coin_type = 'diamond' if is_crystal else 'gold'
                    
                    title_p = item_div.find('p', class_=lambda x: x and 'font-bold' in x and 'text-gray-900' in x)
                    title = title_p.text.strip() if title_p else 'Ball'
                    
                    sub_p = item_div.find('p', class_=lambda x: x and 'text-gray-400' in x and 'text-[13px]' in x)
                    sub = sub_p.text.strip() if sub_p else ''
                    
                    amount_p = item_div.find('p', class_=lambda x: x and 'text-[20px]' in x)
                    amount = amount_p.text.strip() if amount_p else '0'
                    
                    time_p = item_div.find('p', class_=lambda x: x and 'text-[12px]' in x)
                    time_str = time_p.text.strip() if time_p else ''
                    
                    items.append({
                        'type': coin_type,
                        'title': title,
                        'sub': sub,
                        'amount': amount,
                        'time': time_str
                    })
                    
                if items:
                    history.append({
                        'date': date_str,
                        'items': items
                    })
                    
        return {
            'history': history,
            'page': page,
            'total_pages': total_pages
        }

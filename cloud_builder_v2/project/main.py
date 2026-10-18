import re
import threading
from html.parser import HTMLParser

import requests
from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.properties import ListProperty, NumericProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.recycleview.views import RecycleDataViewBehavior

PORT = "3128"
URL = ("https://www.freeproxy.world/?type=&anonymity=&country="
       "&speed=&port={port}&page={page}")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 13; SM-A245F) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

Window.clearcolor = (0.95, 0.97, 1, 1)

KV = """
#:import hex kivy.utils.get_color_from_hex

<RoundBtn@Button>:
    background_normal: ''
    background_color: 0, 0, 0, 0
    bold: True
    color: 1, 1, 1, 1
    canvas.before:
        Color:
            rgba: hex('#3D7BFF') if self.state == 'normal' else hex('#2A5FD6')
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]

<ProxyCard>:
    orientation: 'horizontal'
    size_hint_y: None
    height: dp(78)
    padding: dp(14), dp(8)
    spacing: dp(10)
    canvas.before:
        Color:
            rgba: 1, 1, 1, 1
        RoundedRectangle:
            pos: self.x, self.y + dp(3)
            size: self.width, self.height - dp(6)
            radius: [dp(16)]
        Color:
            rgba: hex('#3D7BFF')
        RoundedRectangle:
            pos: self.x, self.y + dp(3)
            size: dp(6), self.height - dp(6)
            radius: [dp(16), 0, 0, dp(16)]
    BoxLayout:
        orientation: 'vertical'
        Label:
            text: root.ip + ':' + root.port
            color: hex('#1B2A4A')
            bold: True
            font_size: sp(18)
            halign: 'left'
            valign: 'middle'
            text_size: self.size
        Label:
            text: root.info
            color: hex('#6B7A99')
            font_size: sp(12)
            halign: 'left'
            valign: 'middle'
            text_size: self.size
            shorten: True
            shorten_from: 'right'
    Label:
        text: 'COPY'
        size_hint_x: None
        width: dp(56)
        color: hex('#3D7BFF')
        bold: True
        font_size: sp(12)

BoxLayout:
    orientation: 'vertical'
    padding: dp(12)
    spacing: dp(10)

    BoxLayout:
        size_hint_y: None
        height: dp(70)
        padding: dp(16), 0
        canvas.before:
            Color:
                rgba: hex('#3D7BFF')
            RoundedRectangle:
                pos: self.pos
                size: self.size
                radius: [dp(20)]
        BoxLayout:
            orientation: 'vertical'
            Label:
                text: 'Proxy List  -  Port 3128'
                bold: True
                font_size: sp(20)
                color: 1, 1, 1, 1
                halign: 'left'
                valign: 'middle'
                text_size: self.size
            Label:
                text: 'source: freeproxy.world'
                font_size: sp(11)
                color: 1, 1, 1, .85
                halign: 'left'
                valign: 'top'
                text_size: self.size

    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(8)
        TextInput:
            id: search
            hint_text: 'Filter (IP / country)...'
            multiline: False
            font_size: sp(15)
            padding: dp(14), dp(14)
            background_normal: ''
            background_active: ''
            background_color: 1, 1, 1, 1
            foreground_color: hex('#1B2A4A')
            on_text: app.apply_filter(self.text)
        RoundBtn:
            text: 'Refresh'
            size_hint_x: None
            width: dp(100)
            on_release: app.load(1)

    Label:
        text: app.status
        size_hint_y: None
        height: dp(22)
        color: hex('#6B7A99')
        font_size: sp(12)

    RecycleView:
        id: rv
        data: app.items
        viewclass: 'ProxyCard'
        bar_width: dp(3)
        RecycleBoxLayout:
            default_size: None, dp(78)
            default_size_hint: 1, None
            size_hint_y: None
            height: self.minimum_height
            orientation: 'vertical'

    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(8)
        RoundBtn:
            text: '< Prev'
            on_release: app.load(app.page - 1)
        Label:
            text: 'Page ' + str(app.page)
            color: hex('#1B2A4A')
            bold: True
            size_hint_x: None
            width: dp(90)
        RoundBtn:
            text: 'Next >'
            on_release: app.load(app.page + 1)
"""


class TableParser(HTMLParser):
    """Collects text of every <td> in every <tr>."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in_td = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._in_td, self._cell = True, []

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._in_td = False
            self._row.append(" ".join("".join(self._cell).split()))
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_td:
            self._cell.append(data)


def parse_proxies(html):
    p = TableParser()
    p.feed(html)
    out = []
    for cells in p.rows:
        ip = next((c for c in cells if IP_RE.match(c)), None)
        if not ip:
            continue
        rest = [c for c in cells if c and c != ip and c != PORT]
        out.append({"ip": ip, "port": PORT, "info": "  |  ".join(rest)})
    if not out:  # fallback: just grab IPs
        for ip in dict.fromkeys(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", html)):
            out.append({"ip": ip, "port": PORT, "info": ""})
    return out


class ProxyCard(RecycleDataViewBehavior, BoxLayout):
    ip = StringProperty("")
    port = StringProperty("")
    info = StringProperty("")

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            Clipboard.copy(f"{self.ip}:{self.port}")
            App.get_running_app().status = f"Copied {self.ip}:{self.port}"
            return True
        return super().on_touch_down(touch)


class ProxyApp(App):
    title = "Proxy 3128"
    items = ListProperty([])
    status = StringProperty("Tap Refresh to load")
    page = NumericProperty(1)

    def build(self):
        self._all = []
        self.root_widget = Builder.load_string(KV)
        Clock.schedule_once(lambda dt: self.load(1), 0.5)
        return self.root_widget

    def load(self, page):
        if page < 1:
            return
        self.status = "Loading..."
        threading.Thread(target=self._fetch, args=(page,), daemon=True).start()

    def _fetch(self, page):
        try:
            r = requests.get(URL.format(port=PORT, page=page),
                             headers=HEADERS, timeout=20)
            r.raise_for_status()
            data = parse_proxies(r.text)
            if not data:
                raise ValueError("No proxies found (site may be blocking)")
            Clock.schedule_once(lambda dt: self._done(page, data))
        except Exception as e:
            msg = f"Error: {str(e)[:70]}"
            Clock.schedule_once(lambda dt: setattr(self, "status", msg))

    def _done(self, page, data):
        self.page = page
        self._all = data
        self.apply_filter(self.root_widget.ids.search.text)

    def apply_filter(self, text):
        t = text.strip().lower()
        self.items = [d for d in self._all
                      if not t or t in d["ip"] or t in d["info"].lower()]
        self.status = f"{len(self.items)} proxies  -  tap a card to copy"


if __name__ == "__main__":
    ProxyApp().run()

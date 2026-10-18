# ---- APK Cloud Builder crash guard (generated) ----
# Shows errors on the phone screen instead of closing silently. Safe to delete.
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REAL = os.path.join(_HERE, "real_main.py")
_shown = []


def _save(text):
    try:
        try:
            from android.storage import app_storage_path
            base = app_storage_path()
        except Exception:
            base = _HERE
        with open(os.path.join(base, "crash_log.txt"), "a", encoding="utf-8") as f:
            f.write(text + "\n" + "-" * 40 + "\n")
    except Exception:
        pass


def _view(text):
    from kivy.graphics import Color, Rectangle
    from kivy.metrics import dp, sp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.label import Label
    from kivy.uix.scrollview import ScrollView
    from kivy.core.clipboard import Clipboard
    root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
    with root.canvas.before:
        Color(1, 1, 1, 1)
        rect = Rectangle(pos=root.pos, size=root.size)
    root.bind(pos=lambda w, v: setattr(rect, "pos", v), size=lambda w, v: setattr(rect, "size", v))
    root.add_widget(Label(text="The app hit an error (send a screenshot of this page)", bold=True,
                          size_hint_y=None, height=dp(44), font_size=sp(15), color=(0.8, 0.1, 0.1, 1)))
    sv = ScrollView()
    body = Label(text=text[-6000:], size_hint_y=None, color=(0.1, 0.1, 0.1, 1), font_size=sp(11),
                 halign="left", valign="top")
    body.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
    body.bind(texture_size=lambda w, s: setattr(w, "height", s[1]))
    sv.add_widget(body)
    root.add_widget(sv)
    row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
    cp = Button(text="Copy error")
    cp.bind(on_release=lambda *a: Clipboard.copy(text))
    row.add_widget(cp)
    root.add_widget(row)
    return root, row


def _popup(text):
    try:
        from kivy.uix.button import Button
        from kivy.uix.popup import Popup
        root, row = _view(text)
        pop = Popup(title="Error (the app keeps running)", content=root, size_hint=(0.96, 0.92))
        close = Button(text="Close")
        close.bind(on_release=pop.dismiss)
        row.add_widget(close)
        pop.open()
    except Exception:
        traceback.print_exc()


def _fallback(text):
    try:
        from kivy.app import App
        from kivy.core.window import Window
        from kivy.uix.button import Button
        Window.clearcolor = (1, 1, 1, 1)

        class ErrorApp(App):
            def build(self):
                root, row = _view(text)
                close = Button(text="Close")
                close.bind(on_release=lambda *a: self.stop())
                row.add_widget(close)
                return root
        ErrorApp().run()
    except Exception:
        traceback.print_exc()


def _install_handler():
    try:
        from kivy.base import ExceptionHandler, ExceptionManager
        from kivy.clock import Clock

        class Guard(ExceptionHandler):
            def handle_exception(self, inst):
                if isinstance(inst, (SystemExit, KeyboardInterrupt)):
                    return ExceptionManager.RAISE
                text = "".join(traceback.format_exception(type(inst), inst, inst.__traceback__))
                _save(text)
                if len(_shown) < 3:
                    _shown.append(text)
                    Clock.schedule_once(lambda dt, t=text: _popup(t), 0)
                return ExceptionManager.PASS
        ExceptionManager.add_handler(Guard())
    except Exception:
        pass


def _main():
    _install_handler()
    try:
        import runpy
        if not os.path.exists(_REAL):
            raise FileNotFoundError("real_main.py is missing from the APK")
        runpy.run_path(_REAL, run_name="__main__")
    except Exception:
        text = traceback.format_exc()
        _save(text)
        _fallback(text)


_main()

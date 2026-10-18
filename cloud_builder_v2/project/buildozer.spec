[app]
title = Proxy 3128
package.name = proxy3128
package.domain = org.proxyapp
source.dir = .
source.include_exts = py,png,jpg,kv
version = 1.0
requirements = python3,kivy==2.3.0,requests,urllib3,idna,charset-normalizer,certifi
orientation = portrait
fullscreen = 0

android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1

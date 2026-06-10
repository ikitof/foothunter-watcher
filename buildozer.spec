[app]
title = Foot Live
package.name = footlive
package.domain = com.wiriath
source.dir = .
source.include_exts = py,png,csv,md,xml
source.exclude_dirs = .git,.github,.buildozer,.buildozer-global,.android-signing,.claude,android,bin,dist,scripts
source.exclude_patterns = error.jpeg,foot_scores_config.json
version.regex = APP_VERSION = "(.*)"
version.filename = %(source.dir)s/mobile_app.py
requirements = python3,kivy==2.3.1,pyjnius,certifi
icon.filename = %(source.dir)s/foot-live.png
orientation = portrait
fullscreen = 0

android.permissions = INTERNET
android.api = 35
android.minapi = 24
android.ndk_api = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.extra_manifest_application_arguments = android/manifest_application_arguments.xml
android.add_resources = android/res
android.release_artifact = apk
android.debug_artifact = apk

[buildozer]
log_level = 2
warn_on_root = 0
bin_dir = ./bin

# Player Converter — install / package helpers
#
#   make install            install for the current user into ~/.local (no root)
#   make uninstall          remove that install
#   sudo make install PREFIX=/usr/local   system-wide install
#   make deb                build dist/player-converter_<version>_all.deb
#   make check              syntax-check the script and validate the desktop files

APP_ID   := io.github.bigkrisso.PlayerConverter
PREFIX   ?= $(HOME)/.local
DESTDIR  ?=
VERSION  := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' player_converter.py)

BINDIR   := $(DESTDIR)$(PREFIX)/bin
SHAREDIR := $(DESTDIR)$(PREFIX)/share
DEBROOT  := build/deb

.PHONY: all install uninstall deb check clean

all:
	@echo "Targets: install, uninstall, deb, check, clean  (PREFIX=$(PREFIX))"

install:
	install -Dm755 player_converter.py $(BINDIR)/player-converter
	install -d $(SHAREDIR)/applications
	sed 's|^Exec=player-converter|Exec=$(PREFIX)/bin/player-converter|' \
		data/$(APP_ID).desktop > $(SHAREDIR)/applications/$(APP_ID).desktop
	chmod 644 $(SHAREDIR)/applications/$(APP_ID).desktop
	install -Dm644 data/$(APP_ID).svg $(SHAREDIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	install -Dm644 data/$(APP_ID).metainfo.xml $(SHAREDIR)/metainfo/$(APP_ID).metainfo.xml
ifeq ($(DESTDIR),)
	-update-desktop-database -q $(SHAREDIR)/applications 2>/dev/null
	-gtk-update-icon-cache -q -t -f $(SHAREDIR)/icons/hicolor 2>/dev/null
	@echo
	@echo "Installed Player Converter $(VERSION) to $(PREFIX)."
	@echo "Launch it from your app menu, or run: player-converter"
endif

uninstall:
	rm -f $(BINDIR)/player-converter
	rm -f $(SHAREDIR)/applications/$(APP_ID).desktop
	rm -f $(SHAREDIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	rm -f $(SHAREDIR)/metainfo/$(APP_ID).metainfo.xml
	-update-desktop-database -q $(SHAREDIR)/applications 2>/dev/null
	-gtk-update-icon-cache -q -t -f $(SHAREDIR)/icons/hicolor 2>/dev/null

deb:
	rm -rf $(DEBROOT)
	$(MAKE) --no-print-directory install DESTDIR=$(abspath $(DEBROOT)) PREFIX=/usr
	install -Dm644 LICENSE $(DEBROOT)/usr/share/doc/player-converter/copyright
	install -Dm644 README.md $(DEBROOT)/usr/share/doc/player-converter/README.md
	install -d $(DEBROOT)/DEBIAN
	printf '%s\n' \
		"Package: player-converter" \
		"Version: $(VERSION)" \
		"Architecture: all" \
		"Maintainer: bigkrisso <https://github.com/bigkrisso>" \
		"Section: video" \
		"Priority: optional" \
		"Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), ffmpeg" \
		"Homepage: https://github.com/bigkrisso/Player-Converter" \
		"Description: convert movies for an external media player and copy them to a drive" \
		" Makes movies playable on a media player limited to 8-bit H.264 (level 4.1," \
		" up to 1080p30) with AAC audio, using the lightest change possible, then" \
		" copies them onto a removable drive. GTK 4 / libadwaita GUI plus a CLI." \
		> $(DEBROOT)/DEBIAN/control
	mkdir -p dist
	dpkg-deb --root-owner-group --build $(DEBROOT) dist/player-converter_$(VERSION)_all.deb

check:
	python3 -m py_compile player_converter.py
	desktop-file-validate data/$(APP_ID).desktop
	appstreamcli validate --no-net data/$(APP_ID).metainfo.xml

clean:
	rm -rf build dist *.egg-info __pycache__

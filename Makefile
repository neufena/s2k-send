# Build s2kexec on the Raspberry Pi (needs g++). `sudo make install` puts it on PATH for an installed server.
PREFIX ?= /usr/local

s2kexec: s2kexec.cpp gpio-source/gpio_bus.cpp gpio-source/gpio_bus_rpi.cpp gpio-source/gpio_initiator.cpp
	g++ -O2 -std=c++17 -Wall -Igpio-source $^ -o $@

install: s2kexec
	install -D -m 755 s2kexec $(DESTDIR)$(PREFIX)/bin/s2kexec

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/s2kexec

clean:
	rm -f s2kexec
.PHONY: install uninstall clean

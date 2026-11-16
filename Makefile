# Build s2kexec on the Raspberry Pi (needs g++).
s2kexec: s2kexec.cpp gpio-source/gpio_bus.cpp gpio-source/gpio_bus_rpi.cpp gpio-source/gpio_initiator.cpp
	g++ -O2 -std=c++17 -Wall -Igpio-source $^ -o $@

clean:
	rm -f s2kexec
.PHONY: clean

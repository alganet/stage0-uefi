# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

cc = clang
cflags = -ffreestanding -MMD -mno-red-zone -std=c11 -target x86_64-unknown-windows -masm=intel
ld = lld-link
lflags = -subsystem:efi_application -nodefaultlib -dll

build = build

all: $(build)/hex0.efi $(build)/kaem-minimal.efi $(build)/hex1.efi

$(build)/%.o : %.S
	mkdir -p $(build)
	$(cc) $(cflags) -c $< -o $@

$(build)/%.efi : $(build)/%.o
	$(ld) $(lflags) -entry:_start $< -out:$@

. PHONY : clean all
clean:
	rm -rf $(build)

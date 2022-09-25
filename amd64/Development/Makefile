# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

targets = kaem-optional hex0 hex1 hex2 catm M0

cc = clang
cflags = -ffreestanding -MMD -mno-red-zone -std=c11 -target x86_64-unknown-windows -masm=intel
ld = lld-link
lflags = -subsystem:efi_application -nodefaultlib -dll

build = build
targets2 = $(addsuffix .efi, $(addprefix $(build)/, $(targets)))

all: $(targets2)

$(build)/%.o : %.S
	mkdir -p $(build)
	$(cc) $(cflags) -c $< -o $@

$(build)/%.efi : $(build)/%.o
	$(ld) $(lflags) -entry:_start $< -out:$@

. PHONY : clean all
clean:
	rm -rf $(build)

# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

targets = kaem-optional hex0 hex1 hex2 catm M0 cc_amd64

cc = clang
cflags = -ffreestanding -MMD -mno-red-zone -target x86_64-unknown-windows-coff -fno-PIE -fno-stack-protector
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

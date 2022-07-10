# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

	.text
        .globl  efi_main

efi_main:
        sub    $16, %rsp
        mov    %rdx, 8(%rsp)
        mov    %rcx, (%rsp)
        mov    $42, %eax
        add    $16, %rsp
        ret

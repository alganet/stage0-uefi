# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Calling convention:
# First four arguments are passed via registers but we need to leave stack space

.global _start
.text

_start:
        sub  rsp, 64
        mov  [ImageHandle], rcx            # ImageHandle *image_handle
        mov  rax, [rdx+96]                 # system->boot
        mov  [SystemBoot], rax             # save system->boot

        # Open Loaded Image protocol
                                           # arg1 = ImageHandle
        mov  rdx, [LOADED_IMAGE_PROTOCOL]  # EFI_LOADED_IMAGE_PROTOCOL_GUID (first 64 bits)
        mov  [rsp+48], rdx                 # save onto stack
        mov  rdx, [LOADED_IMAGE_PROTOCOL+8]# EFI_LOADED_IMAGE_PROTOCOL_GUID (last 64 bits)
        mov  [rsp+56], rdx                 # save onto stack
        lea  rdx, [rsp+48]                 # arg2 = &EFI_LOADED_IMAGE_PROTOCOL_GUID
                                           # arg3 = &image
        mov  r9, rcx                       # arg4 = image_handle
        mov  qword ptr [rsp+32], 0         # arg5 = NULL
        mov  qword ptr [rsp+40], 1         # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL

        call [rax+280]                     # system->boot->open_protocol();
        add  rsp, 64

        # Command line args

Done:
        ret

.data

ImageHandle: .quad 0
SystemBoot: .quad 0

# Protocol GUIDs
LOADED_IMAGE_PROTOCOL:
.long  0x5b1b31a1
.short 0x9562
.short 0x11d2
.byte 0x8e, 0x3f, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

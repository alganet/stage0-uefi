# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2017 Jeremiah Orians <jeremiah@pdp10.guru>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Usage: hex0 file.hex0 file
# Does not validate the arguments or check for success

# Calling convention:
# First four arguments are passed via registers rcx, rdx, r8, r9 (if they fit in 64-bits)
# but we need to leave stack space

# Registers:
#   r12 in/fin: input file name, later reused for handle
#   r13 out/fout: output file name, later reused for handle
#   r14 system->boot->open_protocol from UEFI, later reused for hex0 algorithm, later reused for rootdir
#   r15 image_handle from UEFI, later reused for hex0 algorithm

.global _start
.text

_start:
        mov  rbp, rsp                      # save stack pointer
        mov  r15, rcx                      # save image_handle
        mov  r14, [rdx+96]                 # system->boot
        mov  r14, [r14+280]                # system->boot->open_protocol

        # Open Loaded Image protocol
        push rax                           # allocate stack for image
        mov  r8, rsp                       # arg3 = &image
        mov  rdx, [LOADED_IMAGE_PROTOCOL+8]# EFI_LOADED_IMAGE_PROTOCOL_GUID (last 64 bits)
        push rdx                           # push last 64 bits onto stack
        mov  rdx, [LOADED_IMAGE_PROTOCOL]  # EFI_LOADED_IMAGE_PROTOCOL_GUID (first 64 bits)
        push rdx                           # push first 64 bits onto stack
        mov  rdx, rsp                      # arg2 = &guid
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        mov  r9, rcx                       # arg4 = image_handle
                                           # arg1 = ImageHandle (already set)
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call r14                           # system->boot->open_protocol(image_handle, &guid, &image, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);
        mov  rax, [rsp+64]                 # get image

        # Command line args
        mov  rcx, rax                      # save image
        mov  rax, [rax+56]                 # options = image->load_options

loop_options1:                             # Skip application name
        add  rax, 2                        # ++options
        mov  bl, [rax]                     # *options
        cmp  bl, 0x20                      # if *options != ' '
        jne  loop_options1                 # then jump

        add  rax, 2                        # ++options
        mov  r12, rax

loop_options2:                             # Skip argv[1]
        add  rax, 2                        # ++options
        mov  bl, [rax]                     # *options
        cmp  bl, 0x20                      # if *options != ' '
        jne  loop_options2                 # then jump

        mov  byte ptr [rax], 0             # *options = 0;
        add  rax, 2                        # ++options
        mov  r13, rax

        # Get root device
        push rax                           # allocate stack for rootfs
        mov  r8, rsp                       # arg3 = &rootfs
        mov  rdx, [SIMPLE_FS_PROTOCOL+8]   # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (last 64 bits)
        push rdx                           # push last 64 bits onto stack
        mov  rdx, [SIMPLE_FS_PROTOCOL]     # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (first 64 bits)
        push rdx                           # push first 64 bits onto stack
        mov  rdx, rsp                      # arg2 = &guid
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        mov  r9, r15                       # arg4 = image_handle
        mov  rcx, [rcx+24]                 # arg1 = root_device = image->device
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call r14                           # system->boot->open_protocol(root_device, &guid, &rootfs, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);
        mov  rcx, [rsp+64]                 # get rootfs

        # Get rootfs
        push rdx                           # allocate stack for rootdir
        mov  rdx, rsp                      # arg2 = &rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootfs->open_volume(rootfs, &rootdir)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop r14                            # save &rootdir

        # Open file for writing
        push rdx                           # allocate stack for fout
        mov  rdx, rsp                      # arg2 = &fout
        push 0                             # arg5 = 0
        push 7                             # to get 0x8000000000000003 we set the rightmost 3 bits
        pop r9                             # and then do right rotation by 1
        ror r9, 1                          # arg4 = EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ

        mov  r8, r13                       # arg3 = out
        mov  rcx, r14                      # arg1 = rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        mov  r13, [rsp+40]                 # get fout

        # Open file for reading
        push rdx                           # allocate stack for fin
        mov  rdx, rsp                      # arg2 = &fin
        push 1                             # arg5 = EFI_FILE_READ_ONLY
        push 1                             # prepare to set arg4 to EFI_FILE_MODE_READ
        pop  r9                            # arg4 = EFI_FILE_MODE_READ
        mov  r8, r12                       # arg3 = in
        mov  rcx, r14                      # arg1 = rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        mov  r12, [rsp+40]                 # get fin

        # Our flag for byte processing
        push -1
        pop r15                            # r15 = -1

        # temp storage for the sum
        xor r14d, r14d                     # r14 = 0

loop:
        # Read a byte
        call read_byte

        # process byte
        call hex

        # Deal with -1 values
        test rax, rax
        jl loop

        # deal with toggle
        test r15, r15
        jge print

        # process first byte of pair
        mov r14, rax
        xor r15d, r15d                     # r15 = 0
        jmp loop

# process second byte of pair
print:
        # update the sum and store in output
        shl r14, 4
        add rax, r14

        # flip the toggle
        push -1
        pop r15                            # r15 = -1

        call write_byte

        jmp loop

hex:
        # Purge Comment Lines (#)
        cmp rax, 35
        je purge_comment

        # Purge Comment Lines (;)
        cmp rax, 59
        je purge_comment

        # deal all ascii less than 0
        cmp rax, 48
        jl ascii_other

        # deal with 0-9
        cmp rax, 58
        jl ascii_num

        # deal with all ascii less than A
        cmp rax, 65
        jl ascii_other

        # deal with A-F
        cmp rax, 71
        jl ascii_high

        # deal with all ascii less than a
        cmp rax, 97
        jl ascii_other

        # deal with a-f
        cmp rax, 103
        jl ascii_low

        # The rest that remains needs to be ignored
        jmp ascii_other

purge_comment:
        # Read a byte
        call read_byte

        # Loop if not LF (works for CR/LF and LF/CR endings too)
        cmp rax, 10
        jne purge_comment

        # Otherwise return -1
        push -1
        pop rax                            # rax = -1
        ret

ascii_num:
        sub rax, 48
        ret

ascii_low:
        sub rax, 87
        ret

ascii_high:
        sub rax, 55
        ret

ascii_other:
        push -1
        pop rax                            # rax = -1
        ret

terminate:
        push rbx                           # allocate stack
        mov  rcx, r12                      # arg1 = fin
        call [rcx+16]                      # fin->close()
        mov  rcx, r13                      # arg1 = fin
        call [rcx+16]                      # fout->close()
abort: # used for debugging only
        mov rsp, rbp                       # restore stack
        ret                                # return to UEFI

read_byte:
        mov  rcx, r12                      # arg1 = fin
        push 1                             # size = 1
        mov  rdx, rsp                      # arg2 = &size
        push rbx                           # allocate stack
        mov  r8, rsp                       # arg3 = &input
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+32]                      # fin->read()
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop  rax                           # save input to rax
        pop  rbx                           # save size to rbx

        # If the file ended (0 bytes read) terminate
        test rbx, rbx                      # if size == 0
        je   terminate                     # then we are done

        ret                                # return

# Writes byte stored in al
write_byte:
        mov  rcx, r13                      # arg1 = fout
        push 1                             # size = 1
        mov  rdx, rsp                      # arg2 = &size
        push rax                           # allocate stack
        mov  r8, rsp                       # arg3 = &output
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+40]                      # fout->write()
        add  rsp, 40                       # deallocate stack

        ret                                # return
.data

# Protocol GUIDs
LOADED_IMAGE_PROTOCOL:
.long  0x5b1b31a1
.short 0x9562
.short 0x11d2
.byte  0x8e, 0x3f, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

SIMPLE_FS_PROTOCOL:
.long  0x0964e5b22
.short 0x6459
.short 0x11d2
.byte  0x8e, 0x39, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

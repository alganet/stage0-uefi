# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2017 Jeremiah Orians <jeremiah@pdp10.guru>
#
# SPDX-License-Identifier: GPL-3.0-or-later

.global _start
.text

# efi_main(void *image_handle, struct efi_system_table *system)
_start:
        mov rbp, rsp                       # save stack pointer
        mov r15, rcx                       # save image_handle
        mov r14, [rdx+96]                  # system->boot
        mov r14, [r14+280]                 # system->boot->open_protocol

        # Open Loaded Image protocol
        push rax                           # allocate stack for image
        mov r8, rsp                        # arg3 = &image
        mov rdx, [rip+LOADED_IMAGE_PROTOCOL+8] # EFI_LOADED_IMAGE_PROTOCOL_GUID (last 64 bits)
        push rdx                           # push last 64 bits onto stack
        mov rdx, [rip+LOADED_IMAGE_PROTOCOL] # EFI_LOADED_IMAGE_PROTOCOL_GUID (first 64 bits)
        push rdx                           # push first 64 bits onto stack
        mov rdx, rsp                       # arg2 = &guid
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        mov r9, rcx                        # arg4 = image_handle
                                           # arg1 = ImageHandle (already set)
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call r14                           # system->boot->open_protocol(image_handle, &guid, &image, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        mov rax, [rsp+64]                  # get image
        mov rcx, rax                       # save image

        # Command line args
        mov rbx, [rax+56]                  # options = image->load_options

loop_options1:                             # Skip application name
        add rbx, 2                         # ++options
        mov al, [rbx]                      # *options
        cmp al, 0x20                       # if *options != ' '
        jne loop_options1                  # then jump

        add rbx, 2                         # ++options
        mov r12, rbx                       # save input file

loop_options2:                             # Skip argv[1]
        add rbx, 2                         # ++options
        mov al, [rbx]                      # *options
        cmp al, 0x20                       # if *options != ' '
        jne loop_options2                  # then jump

        mov byte ptr [rbx], 0              # *options = 0
        add rbx, 2                         # ++options
        mov r13, rbx                       # save output file

        # Get root file system
        push rax                           # allocate stack for rootfs
        mov r8, rsp                        # arg3 = &rootfs
        mov rdx, [rip+SIMPLE_FS_PROTOCOL+8] # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (last 64 bits)
        push rdx                           # push last 64 bits onto stack
        mov rdx, [rip+SIMPLE_FS_PROTOCOL]  # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (first 64 bits)
        push rdx                           # push first 64 bits onto stack
        mov rdx, rsp                       # arg2 = &guid
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        mov r9, r15                        # arg4 = image_handle
        mov rcx, [rcx+24]                  # arg1 = root_device = image->device
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call r14                           # system->boot->open_protocol(root_device, &guid, &rootfs, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        mov  rcx, [rsp+64]                 # get rootfs

        # Get root directory
        push rdx                           # allocate stack for rootdir
        mov rdx, rsp                       # arg2 = &rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootfs->open_volume(rootfs, &rootdir)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop r14                            # save &rootdir

        # Open file for reading
        push rdx                           # allocate stack for fin
        mov rdx, rsp                       # arg2 = &fin
        push 1                             # arg5 = EFI_FILE_READ_ONLY
        push 1                             # prepare to set arg4 to EFI_FILE_MODE_READ
        pop r9                             # arg4 = EFI_FILE_MODE_READ
        mov r8, r12                        # arg3 = in
        mov rcx, r14                       # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        mov rdi, [rsp+40]                  # get fin

        # Open file for writing
        push rdx                           # allocate stack for fout
        mov rdx, rsp                       # arg2 = &fout
        push 0                             # arg5 = 0
        push 7                             # to get 0x8000000000000003 we set the rightmost 3 bits
        pop r9                             # and then do right rotation by 1
        ror r9                             # arg4 = EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ
        mov r8, r13                        # arg3 = out
        mov rcx, r14                       # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        mov rbx, [rsp+40]                  # get fout

        mov r15, -1                        # Our flag for byte processing
        mov r14, 0                         # temp storage for the sum
        mov r13, 0                         # Our starting IP
        call First_pass                    # Process it

        # rewind input file
        mov rcx, rdi                       # Using our input file
        xor edx, edx                       # Offset Zero
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+56]                      # fin->set_position(fin, 0)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack

        mov r15, -1                        # Our flag for byte processing
        mov r14, 0                         # temp storage for the sum
        mov r13, 0                         # Our starting IP
        call Second_pass                   # Process it

        jmp Done


First_pass:
        call Read_byte

        # Deal with EOF
        cmp rax, -4
        je First_pass_done

        # Check for :
        cmp rax, 0x3a
        jne First_pass_0

        # Deal with label
        call StoreLabel

First_pass_0:
        # Check for %
        cmp rax, 0x25
        je First_pass_pointer

        # Deal with everything else
        call hex                           # Process our char

        # Deal with EOF
        cmp rax, -4
        je First_pass_done

        # deal with -1 values
        cmp rax, 0
        jl First_pass

        # deal with toggle
        cmp r15, 0
        je First_pass_1
        add r13, 1                         # Increment IP

First_pass_1:
        not r15
        jmp First_pass

First_pass_pointer:
        # Deal with Pointer to label
        call Read_byte                     # Drop the char
        add r13, 4                         # Increment IP
        jmp First_pass                     # Loop again

First_pass_done:
        RET

hex:
        # deal with EOF
        cmp rax, -4
        je EOF
        # deal with line comments starting with #
        cmp rax, 0x23
        je ascii_comment
        # deal with line comments starting with ;
        cmp rax, 0x3b
        je ascii_comment
        # deal all ascii less than 0
        cmp rax, 0x30
        jl ascii_other
        # deal with 0-9
        cmp rax, 0x3a
        jl ascii_num
        # deal with all ascii less than A
        cmp rax, 0x41
        jl ascii_other
        # deal with A-F
        cmp rax, 0x47
        jl ascii_high
        # deal with all ascii less than a
        cmp rax, 0x61
        jl ascii_other
        # deal with a-f
        cmp rax, 0x67
        jl ascii_low
        # The rest that remains needs to be ignored
        jmp ascii_other

Second_pass:
        call Read_byte

        # Deal with EOF
        cmp rax, -4
        je Second_pass_done

        # Simply drop the label
        cmp rax, 0x3a
        jne Second_pass_0

        call Read_byte
        jmp Second_pass

Second_pass_0:
        # Deal with  pointer
        cmp rax, 0x25
        jne Second_pass_1

        call StorePointer
        jmp Second_pass

Second_pass_1:
        # Deal with everything else
        call hex                           # Process our char

        # Deal with EOF
        cmp rax, -4
        je Second_pass_done

        # deal with -1 values
        cmp rax, 0
        jl Second_pass

        # deal with toggle
        cmp r15, 0
        je print

        # process first byte of pair
        mov r14, rax
        mov r15, 0
        jmp Second_pass

Second_pass_done:
        ret

EOF:
        ret
ascii_num:
        sub rax, 0x30
        ret
ascii_low:
        sub rax, 0x57
        ret
ascii_high:
        sub rax, 0x37
        ret
ascii_other:
        mov rax, -1
        ret
ascii_comment:
        call Read_byte
        cmp rax, 0xd
        je ascii_comment_cr
        cmp rax, 0xa
        jne ascii_comment
ascii_comment_cr:
        mov rax, -1
        ret

# process second byte of pair
print:
        # update the sum and store in output
        shl r14, 4
        add rax, r14

        # flip the toggle
        dec r15                            # rbx = -1

        mov rdx,  1                        # set the size of chars we want
        call print_chars

        add r13, 1                         # Increment IP
        jmp Second_pass


Read_byte:
        mov rcx, rdi                       # arg1 = fin
        push 1                             # size = 1
        mov rdx, rsp                       # arg2 = &size
        push rsi                           # allocate stack
        mov r8, rsp                        # arg3 = &input
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+32]                      # fin->read()
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rax                            # save input to rax
        pop rsi                            # save size to rsi

        # If the file ended (0 bytes read) return EOF
        test esi, esi                      # if size == 0
        jne Read_byte_1
        mov rax, -4                        # Put EOF in rax

Read_byte_1:
        ret                                # return

# Writes byte stored in al
print_chars:
        mov rcx, rbx                       # arg1 = fout
        push rdx                           # size = 1
        mov rdx, rsp                       # arg2 = &size
        push rax                           # allocate stack
        mov r8, rsp                        # arg3 = &output
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+40]                      # fout->write()
        add rsp, 40                        # deallocate stack

        ret                                # return

Get_table_target:
        call Read_byte                     # Get single char label
        shl rax, 3                         # Each label in table takes 8 bytes to store
        add rax, table                     # Calculate offset
        ret

StoreLabel:
        call Get_table_target
        mov [rax], r13                     # Write out pointer to table
        ret

StorePointer:
        add r13, 4                         # Increment IP
        call Get_table_target              # Get address of pointer
        mov rax, [rax]                     # Get pointer
        sub rax, r13                       # target - ip
        mov rdx, 4                         # set the size of chars we want
        call print_chars
        ret

Done:
        push rbx                           # allocate stack
        mov rcx, rdi                       # arg1 = fin
        call [rcx+16]                      # fin->close()
        mov rcx, rbx                       # arg1 = fout
        call [rcx+16]                      # fout->close()
abort: # used for debugging only
        mov rsp, rbp                       # restore stack
        ret                                # return to UEFI

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

table:
.long 0, 0

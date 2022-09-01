# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2017 Jeremiah Orians <jeremiah@pdp10.guru>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Some of the functions are deliberately inlined at the slight expense of
# binary size to avoid tricky jump calculations in hex0 code.

.global _start
.text

# efi_main(void *image_handle, struct efi_system_table *system)
_start:
        mov rbp, rsp                       # save stack pointer
        mov r15, rcx                       # save image_handle
        mov r14, [rdx+96]                  # system->boot

        # Open Loaded Image protocol
        mov r9, r15                        # arg4 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, r9                        # arg1 = image_handle
        push rax                           # allocate stack for image
        mov r8, rsp                        # arg3 = &image
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+280]                     # system->boot->open_protocol(image_handle, &guid, &image, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        mov rdi, [rsp+48]                  # save image

        # Get root file system
        mov r9, r15                        # arg4 = image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL]  # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, [rdi+24]                  # arg1 = root_device = image->device
        mov r13, rcx                       # save root_device
        push rax                           # allocate stack for rootfs
        mov r8, rsp                        # arg3 = &rootfs
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+280]                     # system->boot->open_protocol(root_device, &guid, &rootfs, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        mov rcx, [rsp+48]                  # get rootfs

        # Get root directory
        push rax                           # allocate stack for rootdir
        mov rdx, rsp                       # arg2 = &rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootfs->open_volume(rootfs, &rootdir)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rsi                            # get rootdir

        # Push command line arguments onto stack
        mov rbx, [rdi+56]                  # options = image->load_options
        mov rdx, rbx                       # save beginning of load_options
        add rbx, [rdi+48]                  # go to the end of load_options
        push 0                             # Save end of arguments (NULL) onto stack
loop_options:
        cmp rbx, rdx                       # Check if we are done
        je loop_options_done               # We are done
        sub rbx, 2                         # --options
        mov al, [rbx]                      # *options
        cmp al, 0x20                       # if *options != ' '
        jne loop_options                   # then continue looping
        mov BYTE PTR [rbx], 0              # zero it
        add rbx, 2                         # ++options
        push rbx                           # push another argument onto stack
        jmp loop_options                   # next argument
loop_options_done:

        # Open file for reading
        pop r8                             # arg3 = in
        push rdx                           # allocate stack for fin
        mov rdx, rsp                       # arg2 = &fin
        push 1                             # arg5 = EFI_FILE_READ_ONLY
        push 1                             # prepare to set arg4 to EFI_FILE_MODE_READ
        pop r9                             # arg4 = EFI_FILE_MODE_READ
        mov rcx, rsi                       # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        add rsp, 40                        # deallocate stack
        pop rdi                            # get fin

        # Open file for writing
        pop r8                             # arg3 = out
        push rdx                           # allocate stack for fout
        mov rdx, rsp                       # arg2 = &fout
        push 0                             # arg5 = 0
        push 7                             # to get 0x8000000000000003 we set the rightmost 3 bits
        pop r9                             # and then do right rotation by 1
        ror r9                             # arg4 = EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ
        mov rcx, rsi                       # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        add rsp, 40                        # deallocate stack
        pop rbx                            # get fout

        # Save variables that are needed for cleanup
        push r13                           # save root_device
        push r14                           # save system->boot
        push r15                           # save image_handle
        push rsi                           # save rootdir

        # Allocate pool for single-character label table
        # pointer to table will be stored at the top of the stack
        push rdx                           # allocate stack for table
        mov r8, rsp                        # arg3 = &table
        xor edx, edx                       # zero rdx
        mov dh, 0x8                        # arg2 = 256 * 8 = 2048 = 0x800
        push 2
        pop rcx                            # arg1 = EFI_LOADER_DATA
        sub rsp, 24                        # allocate shadow stack space for UEFI
        call [r14+64]                      # system->boot->allocate_pool(EFI_LOADER_DATA, 2048, &table)
        add rsp, 24                        # deallocate stack

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
        not r15                            # r15 = -1

        mov rdx,  1                        # set the size of chars we want
        call print_chars

        add r13, 1                         # Increment IP
        jmp Second_pass


Read_byte:
        mov rcx, rdi                       # arg1 = fin
        push 1                             # size = 1
        mov rdx, rsp                       # arg2 = &size
        xor esi, esi                       # zero rsi
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

# Writes bytes stored in rax
print_chars:
        mov rcx, rbx                       # arg1 = fout
        push rdx                           # set size
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
        mov rcx, [rsp+24]                  # Get table
        add rax, rcx                       # Calculate offset
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
        pop rcx                            # restore table
        pop rsi                            # restore rootdir
        pop r15                            # restore image_handle
        pop r14                            # restore system->boot
        pop r13                            # restore root_device

        # Free pool
                                           # arg1 = table
        push rax                           # allocate shadow stack space for UEFI function
        call [r14+72]                      # system->boot->free_pool(table)

        mov rcx, rdi                       # arg1 = fin
        call [rcx+16]                      # fin->close()
        mov rcx, rbx                       # arg1 = fout
        call [rcx+16]                      # fout->close()
        mov rcx, rsi                       # arg1 = rootdir
        call [rcx+16]                      # rootdir->close()

        mov r8, r15                        # arg3 = image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL]  # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, r13                       # arg1 = root_device
        xor r9, r9                         # arg4 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+288]                     # system->boot->close_protocol(root_device, &guid, image_handle, 0)

        mov r8, r15                        # arg3 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, r8                        # arg1 = image_handle
        xor r9, r9                         # arg4 = NULL
        call [r14+288]                     # system->boot->close_protocol(image_handle, &guid, image_handle, 0)

abort: # used for debugging only
        mov rsp, rbp                       # restore stack
        ret                                # return to UEFI


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

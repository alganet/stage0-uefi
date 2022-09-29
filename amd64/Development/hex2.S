# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2017 Jeremiah Orians <jeremiah@pdp10.guru>
#
# SPDX-License-Identifier: GPL-3.0-or-later

.intel_syntax noprefix
.global _start
.text

        # Register usage:
        # R15 => Flag
        # R14 => High bits
        # R13 => IP
        # R12 => MALLOC
        # R11 => HEAD

        # Struct format: (size 24)
        # NEXT => 0
        # TARGET => 8
        # NAME => 16

# efi_main(void *image_handle, struct efi_system_table *system)
_start:
        mov rbp, rsp                       # save stack pointer
        mov [rip+image_handle], rcx        # save image_handle
        mov r14, [rdx+96]                  # system->boot
        mov [rip+SystemBoot], r14          # save system->boot

        # Open Loaded Image protocol
        mov r9, [rip+image_handle]         # arg4 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, r9                        # arg1 = image_handle
        call open_protocol                 # open protocol
        mov rdi, rax                       # save image

        # Get root file system
        mov r9, [rip+image_handle]         # arg4 = image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL]  # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, [rdi+24]                  # arg1 = root_device = image->device
        mov [rip+root_device], rcx         # save root_device
        call open_protocol                 # open protocol
        mov rcx, rax                       # get rootfs

        # Get root directory
        lea rdx, [rip+rootdir]             # arg2 = &rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootfs->open_volume(rootfs, &rootdir)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack

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
        push 1                             # Set exit code in case of failure
        cmp r8, 0                          # If NULL
        je failed_input                    # then exit
        lea rdx, [rip+fin]                 # arg2 = &fin
        push 1                             # arg5 = EFI_FILE_READ_ONLY
        push 1                             # prepare to set arg4 to EFI_FILE_MODE_READ
        pop r9                             # arg4 = EFI_FILE_MODE_READ
        mov rcx, [rip+rootdir]             # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        cmp rax, 0                         # If failed to open
        jne failed_input                   # then exit
        add rsp, 48                        # deallocate stack

        # Open file for writing
        pop r8                             # arg3 = out
        push 1                             # Set exit code in case of failure
        cmp r8, 0                          # If NULL
        je failed_output                   # then exit
        lea rdx, [rip+fout]                # arg2 = &fout
        push 0                             # arg5 = 0
        push 7                             # to get 0x8000000000000003 we set the rightmost 3 bits
        pop r9                             # and then do right rotation by 1
        ror r9                             # arg4 = EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ
        mov rcx, [rip+rootdir]             # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        add rsp, 48                        # deallocate stack

        # Allocate ourselves 16 MiB of memory
        mov rdx, 0x1000000                 # allocate 16 MiB of memory
        call allocate_pool
        mov [rip+scratch], rax             # Allocate space for scratch area
        add rax, 0x800                     # 2 KiB of scratch
        mov r12, rax                       # save structs pointer

        call ClearScratch                  # Zero scratch
        mov r15, -1                        # Our flag for byte processing
        mov r14, 0                         # temp storage for the sum
        mov r13, 0x00600000                # Our starting IP
        mov r11, 0                         # HEAD = NULL
        call First_pass                    # Process it

        # rewind input file
        push r11                           # Protect r11
        mov rcx, [rip+fin]                 # Using our input file
        xor edx, edx                       # Offset Zero
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+56]                      # fin->set_position(fin, 0)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop r11                            # restore r11

        mov r15, -1                        # Our flag for byte processing
        mov r14, 0                         # temp storage for the sum
        mov r13, 0x00600000                # Our starting IP
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
        jmp StoreLabel

First_pass_0:
        # Check for !
        cmp rax, 0x21
        je First_pass_pointer

        # Check for @
        cmp rax, 0x40
        je First_pass_pointer

        # Check for $
        cmp rax, 0x24
        je First_pass_pointer

        # Check for %
        cmp rax, 0x25
        je First_pass_pointer

        # Check for &
        cmp rax, 0x26
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

Update_Pointer:
        # Check for !
        cmp rax, 0x21
        je Update_Pointer_1

        # Check for @
        cmp rax, 0x40
        je Update_Pointer_2

        # Check for $
        cmp rax, 0x24
        je Update_Pointer_2

        # Check for %
        cmp rax, 0x25
        je Update_Pointer_4

        # Check for &
        cmp rax, 0x26
        je Update_Pointer_4

        # deal with bad input
        call fail

Update_Pointer_4:
        add r13, 2                         # Increment IP
Update_Pointer_2:
        add r13, 1                         # Increment IP
Update_Pointer_1:
        add r13, 1                         # Increment IP
        ret

First_pass_pointer:
        # Deal with Pointer to label
        call Update_Pointer                # Increment IP
        mov rbx, [rip+scratch]             # Using scratch
        call consume_token                 # Read token
        call ClearScratch                  # Throw away token
        cmp rax, 0x3E                      # check for '>'
        jne First_pass                     # Loop again

        # Deal with %label>label case
        mov rbx, [rip+scratch]             # Write to scratch
        call consume_token                 # get token
        call ClearScratch                  # Clean up after ourselves
        jmp First_pass                     # Loop again

First_pass_done:
        ret

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

        mov rbx, [rip+scratch]             # Using scratch
        call consume_token                 # Read token
        call ClearScratch                  # Throw away token

        jmp Second_pass

Second_pass_0:
        # Deal with % pointer
        cmp rax, 0x25
        je StorePointer_rel4

        # Deal with @ pointer
        cmp rax, 0x40
        je StorePointer_rel2

        # Deal with ! pointer
        cmp rax, 0x21
        je StorePointer_rel1

        # Deal with & pointer
        cmp rax, 0x26
        je StorePointer_abs4

        # Deal with $ pointer
        cmp rax, 0x24
        je StorePointer_abs2

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

        mov rdx, 1                         # set the size of chars we want
        call print_chars

        add r13, 1                         # Increment IP
        jmp Second_pass


Read_byte:
        push r11                           # Protect r11
        mov rcx, [rip+fin]                 # arg1 = fin
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
        pop r11                            # restore r11

        # If the file ended (0 bytes read) return EOF
        test esi, esi                      # if size == 0
        jne Read_byte_1
        mov rax, -4                        # Put EOF in rax

Read_byte_1:
        ret                                # return

# Writes bytes stored in rax
print_chars:
        push r11                           # Protect r11
        mov rcx, [rip+fout]                # arg1 = fout
        push rdx                           # set size
        mov rdx, rsp                       # arg2 = &size
        push rax                           # allocate stack
        mov r8, rsp                        # arg3 = &output
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+40]                      # fout->write()
        add rsp, 40                        # deallocate stack
        pop r11                            # restore r11

        ret                                # return

        # Receives pointer in RBX
        # Writes out char and updates RBX
consume_token:
        call Read_byte                     # Consume_token

        # Check for \t
        cmp rax, 0x09
        je consume_token_done

        # Check for \n
        cmp rax, 0x0A
        je consume_token_done

        # Check for ' '
        cmp rax, 0x20
        je consume_token_done

        # Check for '>'
        cmp rax, 0x3E
        je consume_token_done

        # Looks like we are still reading token
        mov [rbx], al                      # Store char
        add rbx, 1                         # Point to next spot
        jmp consume_token                  # loop until done

consume_token_done:
        mov rcx, 0                         # Pad with nulls
        mov [rbx], rcx
        add rbx, 8
        ret

StoreLabel:
        mov rax, r12                       # ENTRY
        add r12, 24                        # CALLOC
        mov [rax+8], r13                   # ENTRY->TARGET = IP
        mov [rax], r11                     # ENTRY->NEXT = JUMP_TABLE
        mov r11, rax                       # JUMP_TABLE = ENTRY
        mov [r11+16], r12                  # ENTRY->NAME = TOKEN
        mov rbx, r12                       # Write Starting after struct
        call consume_token                 # Collect whole string
        mov r12, rbx                       # Update HEAP
        jmp First_pass

GetTarget:
        mov rdi, [rip+scratch]             # Reset scratch
        mov rcx, r11                       # Grab JUMP_TABLE
        mov rsi, [rcx+16]                  # I->NAME
GetTarget_loop:
        mov al, [rsi]                      # I->NAME[0]
        mov bl, [rdi]                      # scratch[0]
        movzx rbx, bl                      # Zero extend
        movzx rax, al                      # Zero extend
        cmp al, bl                         # IF TOKEN == I->NAME
        jne GetTarget_miss                 # Oops

        add rsi, 1
        add rdi, 1
        cmp al, 0
        jne GetTarget_loop                 # Loop until
        jmp GetTarget_done                 # Match

        # Miss
GetTarget_miss:
        mov rcx, [rcx]                     # I = I->NEXT
        cmp rcx, 0                         # IF NULL == I
        je fail                            # Abort hard

        mov rsi, [rcx+16]                  # I->NAME
        mov rdi, [rip+scratch]             # Reset scratch
        jmp GetTarget_loop

GetTarget_done:
        mov rax, [rcx+8]                   # Get address
        ret

ClearScratch:
        push rax                           # Protect against changes
        push rbx                           # And overwrites
        push rcx
        push rdx                           # While we work
        mov rbx, [rip+scratch]             # Where our scratch area is
        mov al, 0                          # Using null

        mov rdx, rbx                       # Get scratch
        add rdx, 0x800                     # end of scratch area

ClearScratch_loop:
        cmp rbx, rdx                       # Make sure
        je ClearScratch_end                # we do not overflow

        mov rcx, [rbx]                     # Get current value
        mov [rbx], al                      # Because we want null
        add rbx, 1                         # Increment
        cmp rcx, 0                         # Check if we hit null
        jne ClearScratch_loop              # Keep looping

ClearScratch_end:
        pop rdx
        pop rcx                            # Don't Forget to
        pop rbx                            # Restore Damage
        pop rax                            # Entirely
        ret

StorePointer:
        call Update_Pointer                # Increment IP
        mov rbx, [rip+scratch]             # Write to scratch
        call consume_token                 # get token
        push rax                           # Protect base_sep_p
        mov rax, [rip+scratch]             # Pointer to scratch
        call GetTarget                     # Get address of pointer
        call ClearScratch                  # Clean up after ourselves
        mov rdx, r13                       # base = IP
        pop rbx                            # Restore base_sep_p
        cmp rbx, 0x3E                      # If base_sep_p == '>'
        jne StorePointer_done              # If not

        # Deal with %label>label case
        push rax                           # We need to preserve main target
        mov rbx, [rip+scratch]             # Write to scratch
        call consume_token                 # get token
        mov rax, [rip+scratch]             # Pointer to scratch
        call GetTarget                     # Get address of pointer
        call ClearScratch                  # Clean up after ourselves
        mov rdx, rax                       # Use our new base
        pop rax                            # Restore main target

StorePointer_done:
        ret

StorePointer_rel4:
        call StorePointer                  # Do Common
        sub rax, rdx                       # target - ip
        mov rdx, 4                         # set the size of chars we want
        call print_chars
        jmp Second_pass

StorePointer_rel2:
        call StorePointer                  # Do Common
        sub rax, rdx                       # target - ip
        mov rdx, 2                         # set the size of chars we want
        call print_chars
        jmp Second_pass

StorePointer_rel1:
        call StorePointer                  # Do Common
        sub rax, rdx                       # target - ip
        mov rdx, 1                         # set the size of chars we want
        call print_chars
        jmp Second_pass

StorePointer_abs4:
        call StorePointer                  # Do Common
        mov rdx, 4                         # set the size of chars we want
        call print_chars
        jmp Second_pass

StorePointer_abs2:
        call StorePointer                  # Do Common
        mov rdx, 2                         # set the size of chars we want
        call print_chars
        jmp Second_pass

fail:
        mov rax, 1                         # Set exit code 1
        jmp terminate

Done:
        xor eax, eax                       # Set exit code 0

terminate:
        # Free pool
        push rax                           # save exit code

        mov r14, [rip+SystemBoot]          # get system->boot
        mov rcx, [rip+scratch]             # arg1 = scratch
        push rax                           # allocate shadow stack space for UEFI function
        call [r14+72]                      # system->boot->free_pool(scratch)
        pop rax                            # deallocate stack

        mov rcx, [rip+fout]                # get fout
        call close_file                    # close fout

failed_output:
        mov rcx, [rip+fin]                 # get fin
        call close_file                    # close fin

failed_input:
        mov rcx, [rip+rootdir]             # get rootdir
        call close_file                    # close rootdir

        mov r8, [rip+image_handle]         # arg3 = image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL]  # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, [rip+root_device]         # arg1 = root_device
        call close_protocol                # close protocol

        mov r8, [rip+image_handle]         # arg3 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, r8                        # arg1 = image_handle
        call close_protocol                # close protocol

        pop rax                            # restore exit code

abort: # used for debugging only
        mov rsp, rbp                       # restore stack
        ret                                # return to UEFI

# rcx: file handle
close_file:
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+16]                      # file_handle->close(file_handle)
        pop rax                            # deallocate stack
        ret

# rcx: handle
# rdx: &guid
# r9: agent_handle
# returns interface
open_protocol:
        push rax                           # allocate stack for interface
        mov r8, rsp                        # arg3 = &interface
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+280]                     # system->boot->open_protocol(handle, &guid, &interface, agent_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        add rsp, 48                        # deallocate stack
        pop rax                            # get interface
        ret

# rcx: handle
# rdx: &guid
# r8: agent_handle
close_protocol:
        xor r9, r9                         # arg4 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+288]                     # system->boot->close_protocol(handle, &guid, agent_handle, 0)
        add rsp, 32                        # deallocate stack
        ret

# rdx: number of bytes to allocate
# r14: system->boot
# returns pointer in rax
allocate_pool:
        push rdx                           # allocate stack for pool pointer
        mov r8, rsp                        # arg3 = &pool
        push 2
        pop rcx                            # arg1 = EFI_LOADER_DATA
        sub rsp, 24                        # allocate shadow stack space for UEFI
        call [r14+64]                      # system->boot->allocate_pool(EFI_LOADER_DATA, 2048, &pool)
        add rsp, 24                        # deallocate stack
        pop rax                            # get pool
        ret


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

fin:
.long 0, 0

fout:
.long 0, 0

scratch:
.long 0, 0

rootdir:
.long 0, 0

SystemBoot:
.long 0, 0

image_handle:
.long 0, 0

root_device:
.long 0, 0

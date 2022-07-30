/* SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
 *
 * SPDX-License-Identifier: GPL-3.0-or-later */

/* Written in a low level C that is close to assembly.
 * We skip error checking since this is a prototype for hex0 code */

#include "efi/efi.h"

efi_status_t efi_main(efi_handle_t image_handle, struct efi_system_table *system)
{
    struct efi_loaded_image_protocol *image;
    struct efi_simple_file_system_protocol *rootfs;
    struct efi_file_protocol *rootdir;
    struct efi_guid guid1 = EFI_LOADED_IMAGE_PROTOCOL_GUID;
    struct efi_guid guid2 = EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID;

    /* Open Loaded Image protocol */
    system->boot->open_protocol(image_handle, &guid1, (void **) &image, image_handle, 0,
                                EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);

    /* Command line args */
    uint16_t *options = image->load_options;
    uint16_t *in;
    uint16_t *out;
    do {
        ++options;
    } while (*options != ' '); /* Skip application name */
    in = ++options;
    do {
        ++options;
    } while (*options != ' ');
    *options = 0;
    out = ++options;

    /* Get root file system */
    efi_handle_t root_device = image->device;
    system->boot->open_protocol(root_device, &guid2, (void **) &rootfs, image_handle, 0,
                                EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);
    /* Get root directory */
    rootfs->open_volume(rootfs, &rootdir);

    /* Open file for writing */
    struct efi_file_protocol *fout;
    rootdir->open(rootdir, &fout, out, EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ, 0);

    /* Open file for reading */
    struct efi_file_protocol *fin;
    rootdir->open(rootdir, &fin, in, EFI_FILE_MODE_READ, EFI_FILE_READ_ONLY);

    uint8_t c;
    uint64_t size;

    uint8_t toggle = 0;
    uint8_t hold;

next_byte:
    size = 1;
    fin->read(fin, &size, &c);

    /* If the file ended (0 bytes read) terminate */
    if (size == 0) {
        goto terminate;
    }

    /* Check if it's a comment */
    if (c != '#' && c != ';') {
        goto not_comment;
    }

    loop:
        fin->read(fin, &size, &c);
        /* If the file ended (0 bytes read) terminate */
        if (size == 0) {
            goto terminate;
        }

        /* Check if read byte is the end of the comment (i.e. a newline character),
         * in that case we continue processing */
        if (c == '\n') {
            goto next_byte;
        }
    goto loop;

not_comment:
    /* Check if it's a hex character:
     * in the case it's not, ignores and reads next byte */

    if (c >= '0' && c <= '9') {
        c -= 48;
    }
    else if (c >= 'A' && c <= 'F') {
        c -= 55;
    }
    else if (c >= 'a' && c <= 'f') {
        c -= 87;
    }
    else {
        goto next_byte;
    }

    if (!toggle) {
        hold = c;
        toggle = 1;
    }
    else {
        c = (hold << 4) + c;
        fout->write(fout, &size, &c);
        hold = 0;
        toggle = 0;
    }

    goto next_byte;

terminate:
    fin->close(fin);
    fout->close(fout);

    return 0;
}

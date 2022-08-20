/* SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
 *
 * SPDX-License-Identifier: GPL-3.0-or-later */

#include "efi/efi.h"

#define EOF -4
#define EXIT_FAILURE 1
#define TRUE 1
#define FALSE 0
#define max_string 4096

typedef struct efi_file_protocol FILE;

struct entry
{
    struct entry* next;
    unsigned target;
    char* name;
};

/* Globals */
FILE* output;
struct entry* jump_table;
int Base_Address;
int ip;
char* scratch;
struct efi_boot_table *boot;
efi_handle_t efi_image_handle;
struct efi_simple_text_output_protocol *stdout;
char c;

char fgetc(FILE* fin)
{
    uint64_t size = 1;
    fin->read(fin, &size, &c);
    if (size == 0) {
        c = EOF;
    }
    uint16_t temp;
    temp = c;
    return c;
}

__attribute__((noreturn)) void exit(efi_status_t exit_code)
{
    boot->exit(efi_image_handle, exit_code, 0);
    __builtin_unreachable();
}

void* memset(void* ptr, int value, int num)
{
    char* s;
    for(s = ptr; 0 < num; num = num - 1)
    {
        s[0] = value;
        s = s + 1;
    }

    return ptr;
}

void* malloc(unsigned size)
{
    uint16_t *pool;
    boot->allocate_pool(EFI_LOADER_DATA, size, (void **) &pool);
    return pool;
}


void* calloc(int count, int size)
{
    void* ret = malloc(count * size);
    if(NULL == ret) return NULL;
    memset(ret, 0, (count * size));
    return ret;
}

char fputc(char c, FILE* fout)
{
    uint64_t size = 1;
    fout->write(fout, &size, &c);
    return c;
}

int match(char* a, char* b)
{
    if((NULL == a) && (NULL == b)) return TRUE;
    if(NULL == a) return FALSE;
    if(NULL == b) return FALSE;

    int i = -1;
    do
    {
        i = i + 1;
        if(a[i] != b[i])
        {
            return FALSE;
        }
    } while((0 != a[i]) && (0 !=b[i]));
    return TRUE;
}


int in_set(int c, char* s)
{
    /* NULL set is always false */
    if(NULL == s) return FALSE;

    while(0 != s[0])
    {
        if(c == s[0]) return TRUE;
        s = s + 1;
    }
    return FALSE;
}


int consume_token(FILE* source_file)
{
    int i = 0;
    int c = fgetc(source_file);
    while(!in_set(c, " \t\n>"))
    {
        scratch[i] = c;
        i = i + 1;
        c = fgetc(source_file);
        if(EOF == c) break;
    }

    return c;
}

int Throwaway_token(FILE* source_file)
{
    int c;
    do
    {
        c = fgetc(source_file);
        if(EOF == c) break;
    } while(!in_set(c, " \t\n>"));

    return c;
}

int length(char* s)
{
    int i = 0;
    while(0 != s[i]) i = i + 1;
    return i;
}

void Clear_Scratch(char* s)
{
    do
    {
        s[0] = 0;
        s = s + 1;
    } while(0 != s[0]);
}

void Copy_String(char* a, char* b)
{
    while(0 != a[0])
    {
        b[0] = a[0];
        a = a + 1;
        b = b + 1;
    }
}

unsigned GetTarget(char* c)
{
    struct entry* i;
    for(i = jump_table; NULL != i; i = i->next)
    {
        if(match(c, i->name))
        {
            return i->target;
        }
    }
    exit(EXIT_FAILURE);
}

int storeLabel(FILE* source_file, int ip)
{
    struct entry* entry = calloc(1, sizeof(struct entry));

    /* Ensure we have target address */
    entry->target = ip;

    /* Prepend to list */
    entry->next = jump_table;
    jump_table = entry;

    /* Store string */
    int c = consume_token(source_file);
    entry->name = calloc(length(scratch) + 1, sizeof(char));
    Copy_String(scratch, entry->name);
    Clear_Scratch(scratch);

    return c;
}

void outputPointer(int displacement, int number_of_bytes)
{
    unsigned value = displacement;

    while(number_of_bytes > 0)
    {
        unsigned byte = value % 256;
        value = value / 256;
        fputc(byte, output);
        number_of_bytes = number_of_bytes - 1;
    }
}

void Update_Pointer(char ch)
{
    /* Calculate pointer size*/
    if(in_set(ch, "%&")) ip = ip + 4; /* Deal with % and & */
    else if(in_set(ch, "@$")) ip = ip + 2; /* Deal with @ and $ */
    else if('!' == ch) ip = ip + 1; /* Deal with ! */
    else exit(EXIT_FAILURE);
}

void storePointer(char ch, FILE* source_file)
{
    /* Get string of pointer */
    Clear_Scratch(scratch);
    Update_Pointer(ch);
    int base_sep_p = consume_token(source_file);

    /* Lookup token */
    int target = GetTarget(scratch);
    int displacement;

    int base = ip;

    /* Change relative base address to :<base> */
    if ('>' == base_sep_p)
    {
        Clear_Scratch(scratch);
        consume_token (source_file);
        base = GetTarget (scratch);
    }

    displacement = target - base;

    /* output calculated difference */
    if('!' == ch) outputPointer(displacement, 1); /* Deal with ! */
    else if('$' == ch) outputPointer(target, 2); /* Deal with $ */
    else if('@' == ch) outputPointer(displacement, 2); /* Deal with @ */
    else if('&' == ch) outputPointer(target, 4); /* Deal with & */
    else if('%' == ch) outputPointer(displacement, 4);  /* Deal with % */
    else exit(EXIT_FAILURE);
}

void line_Comment(FILE* source_file)
{
    int c = fgetc(source_file);
    while(!in_set(c, "\n\r"))
    {
        if(EOF == c) break;
        c = fgetc(source_file);
    }
}

int hex(int c, FILE* source_file)
{
    if (in_set(c, "0123456789")) return (c - 48);
    else if (in_set(c, "abcdef")) return (c - 87);
    else if (in_set(c, "ABCDEF")) return (c - 55);
    else if (in_set(c, "#;")) line_Comment(source_file);
    return -1;
}


int hold;
int toggle;
void process_byte(char c, FILE* source_file, int write)
{
    if(0 <= hex(c, source_file))
    {
        if(toggle)
        {
            if(write) fputc(((hold * 16)) + hex(c, source_file), output);
            ip = ip + 1;
            hold = 0;
        }
        else
        {
            hold = hex(c, source_file);
        }
        toggle = !toggle;
    }
}

void first_pass(FILE* input)
{
    toggle = FALSE;
    int c;
    for(c = fgetc(input); EOF != c; c = fgetc(input))
    {
        /* Check for and deal with label */
        if(':' == c)
        {
            c = storeLabel(input, ip);
        }

        /* check for and deal with relative/absolute pointers to labels */
        if(in_set(c, "!@$~%&"))
        { /* deal with 1byte pointer !; 2byte pointers (@ and $); 3byte pointers ~; 4byte pointers (% and &) */
            Update_Pointer(c);
            c = Throwaway_token(input);
            if ('>' == c)
            { /* deal with label>base */
                c = Throwaway_token(input);
            }
        }
        else process_byte(c, input, FALSE);
    }
}

void second_pass(FILE* input)
{
    toggle = FALSE;
    hold = 0;

    int c;
    for(c = fgetc(input); EOF != c; c = fgetc(input))
    {
        if(':' == c) c = Throwaway_token(input); /* Deal with : */
        else if(in_set(c, "!@$~%&")) storePointer(c, input);  /* Deal with !, @, $, ~, % and & */
        else process_byte(c, input, TRUE);
    }
}

void rewind(FILE* f)
{
    f->set_position(f, 0);
}

efi_status_t efi_main(efi_handle_t image_handle, struct efi_system_table *system)
{
    struct efi_loaded_image_protocol *image;
    struct efi_simple_file_system_protocol *rootfs;
    struct efi_file_protocol *rootdir;
    struct efi_guid guid1 = EFI_LOADED_IMAGE_PROTOCOL_GUID;
    struct efi_guid guid2 = EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID;
    boot = system->boot;
    efi_image_handle = image_handle;
    stdout = system->out;

    /* Open Loaded Image protocol */
    boot->open_protocol(image_handle, &guid1, (void **) &image, image_handle, 0,
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
    boot->open_protocol(root_device, &guid2, (void **) &rootfs, image_handle, 0,
                                EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);
    /* Get root directory */
    rootfs->open_volume(rootfs, &rootdir);

    /* Open file for writing */
    struct efi_file_protocol *fout;
    rootdir->open(rootdir, &fout, out, EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ, 0);

    /* Open file for reading */
    struct efi_file_protocol *input;
    rootdir->open(rootdir, &input, in, EFI_FILE_MODE_READ, EFI_FILE_READ_ONLY);

    jump_table = NULL;
    Base_Address = 0x00600000;
    output = fout;
    scratch = calloc(max_string + 1, sizeof(char));

    /* Get all of the labels */
    ip = Base_Address;
    first_pass(input);
    rewind(input);

    /* Fix all the references*/
    ip = Base_Address;
    second_pass(input);

    input->close(input);
    output->close(output);
    rootdir->close(rootdir);
    boot->close_protocol(root_device, &guid2, image_handle, 0);
    boot->close_protocol(image_handle, &guid1, image_handle, 0);
    return 0;
}

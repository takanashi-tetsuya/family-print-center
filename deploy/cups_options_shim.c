/* Merge validated, per-job settings into LibreOffice's CUPS submission.
 * LibreOffice still opens and prints the original document. Its renderer owns
 * copy counts and page ranges; CUPS owns device/filter options such as DPI.
 */
#define _GNU_SOURCE
#if __has_include(<cups/cups.h>)
#include <cups/cups.h>
#else
/* Public CUPS 2.x ABI, also available on development hosts which only have the
 * libcups runtime. Container builds use the distribution's canonical header.
 * https://github.com/OpenPrinting/cups/blob/v2.4.10/cups/cups.h */
typedef struct _http_s http_t;
typedef struct cups_option_s { char *name; char *value; } cups_option_t;
extern int cupsAddOption(const char *, const char *, int, cups_option_t **);
extern int cupsRemoveOption(const char *, int, cups_option_t **);
extern const char *cupsGetOption(const char *, int, cups_option_t *);
extern void cupsFreeOptions(int, cups_option_t *);
#endif
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int read_options(cups_option_t **options) {
    const char *path = getenv("FPC_CUPS_OPTIONS_FILE");
    struct stat status;
    unsigned char header[6];
    int count = 0, records = 0;
    int descriptor = path ? open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC) : -1;
    if (descriptor < 0 || fstat(descriptor, &status) || !S_ISREG(status.st_mode) ||
        status.st_uid != getuid() || (status.st_mode & 0077) || status.st_size > 131072) {
        if (descriptor >= 0) close(descriptor);
        return -1;
    }
    FILE *file = fdopen(descriptor, "rb");
    if (!file) { close(descriptor); return -1; }
    if (fread(header, 1, 6, file) != 6 || memcmp(header, "FPCO1\n", 6)) goto invalid;
    for (;;) {
        unsigned char lengths[4];
        size_t amount = fread(lengths, 1, 4, file);
        if (!amount && feof(file)) break;
        if (amount != 4 || ++records > 512) goto invalid;
        size_t key_length = ((size_t) lengths[0] << 8) | lengths[1];
        size_t value_length = ((size_t) lengths[2] << 8) | lengths[3];
        char key[129], value[2049];
        if (!key_length || key_length > 128 || !value_length || value_length > 2048 ||
            fread(key, 1, key_length, file) != key_length ||
            fread(value, 1, value_length, file) != value_length) goto invalid;
        key[key_length] = value[value_length] = 0;
        for (size_t i = 0; i < key_length; i++)
            if (!((key[i] >= 'a' && key[i] <= 'z') || (key[i] >= 'A' && key[i] <= 'Z') ||
                  (key[i] >= '0' && key[i] <= '9') || (key[i] && strchr("_.-", key[i])))) goto invalid;
        for (size_t i = 0; i < value_length; i++)
            if ((unsigned char) value[i] < 32 || (unsigned char) value[i] == 127) goto invalid;
        /* These have already been applied to the original by document.print(). */
        if (!strcasecmp(key, "copies") || !strcasecmp(key, "page-ranges")) goto invalid;
        count = cupsAddOption(key, value, count, options);
    }
    fclose(file);
    return count;
invalid:
    fclose(file);
    cupsFreeOptions(count, *options);
    *options = NULL;
    return -1;
}

static int merge_options(const char *name, const char *title, int original_count,
                         cups_option_t *original, cups_option_t **merged) {
    const char *printer = getenv("FPC_CUPS_PRINTER");
    const char *prefix = getenv("FPC_CUPS_JOB_PREFIX");
    if (!printer || !prefix || !name || !title || strcmp(name, printer) ||
        strncmp(title, prefix, strlen(prefix))) return -1;
    cups_option_t *selected = NULL;
    int selected_count = read_options(&selected), count = 0;
    if (selected_count < 0) return -1;
    for (int i = 0; i < original_count; i++)
        count = cupsAddOption(original[i].name, original[i].value, count, merged);
    /* Manual media must beat the PageSize emitted for the document's pages.
     * In Auto, no media option exists and the renderer's page size survives. */
    if (cupsGetOption("media", selected_count, selected)) {
        count = cupsRemoveOption("PageSize", count, merged);
        count = cupsRemoveOption("PageRegion", count, merged);
    }
    /* The renderer also emits document/driver defaults. A generic manual
     * choice must beat these, then its verified PPD alias is added below. */
    static const char *equivalents[][2] = {
        {"sides", "Duplex"}, {"print-color-mode", "ColorModel"},
        {"media-source", "InputSlot"}, {"media-type", "MediaType"}, {"media-type", "BrMediaType"},
        {"output-bin", "OutputBin"}, {"collate", "Collate"},
        {"printer-resolution", "Resolution"}, {"printer-resolution", "SetResolution"},
        {"printer-resolution", "JCLResolution"}, {"printer-resolution", "CNRes_PGP"},
        {"print-quality", "cupsPrintQuality"}
    };
    for (size_t i = 0; i < sizeof(equivalents) / sizeof(equivalents[0]); i++)
        if (cupsGetOption(equivalents[i][0], selected_count, selected))
            count = cupsRemoveOption(equivalents[i][1], count, merged);
    for (int i = 0; i < selected_count; i++)
        count = cupsAddOption(selected[i].name, selected[i].value, count, merged);
    cupsFreeOptions(selected_count, selected);
    return count;
}

static void receipt(int job_id) {
    const char *path = getenv("FPC_CUPS_RECEIPT");
    int descriptor = path ? open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600) : -1;
    if (descriptor >= 0) {
        char value[32];
        int size = snprintf(value, sizeof(value), "%d\n", job_id);
        ssize_t written = size > 0 ? write(descriptor, value, (size_t) size) : -1;
        close(descriptor);
        if (written != size) (void) unlink(path);
    }
}

#define WRAPPER(function, signature, arguments) \
int function signature { \
    typedef int (*submit_type) signature; \
    submit_type submit = (submit_type) dlsym(RTLD_NEXT, #function); \
    cups_option_t *merged = NULL; \
    int count = merge_options(name, title, num_options, options, &merged); \
    if (!submit || count < 0) { errno = EINVAL; receipt(0); return 0; } \
    int job_id = submit arguments; \
    cupsFreeOptions(count, merged); \
    receipt(job_id); \
    return job_id; \
}

WRAPPER(cupsPrintFile,
        (const char *name, const char *filename, const char *title, int num_options, cups_option_t *options),
        (name, filename, title, count, merged))
WRAPPER(cupsPrintFile2,
        (http_t *http, const char *name, const char *filename, const char *title, int num_options, cups_option_t *options),
        (http, name, filename, title, count, merged))
WRAPPER(cupsPrintFiles,
        (const char *name, int num_files, const char **files, const char *title, int num_options, cups_option_t *options),
        (name, num_files, files, title, count, merged))
WRAPPER(cupsPrintFiles2,
        (http_t *http, const char *name, int num_files, const char **files, const char *title, int num_options, cups_option_t *options),
        (http, name, num_files, files, title, count, merged))

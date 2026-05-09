#include <locale.h>
#include <stdlib.h>

long long strtoll_l(
    const char *nptr,
    char **endptr,
    int base,
    locale_t locale
) {
    (void)locale;
    return strtoll(nptr, endptr, base);
}

unsigned long long strtoull_l(
    const char *nptr,
    char **endptr,
    int base,
    locale_t locale
) {
    (void)locale;
    return strtoull(nptr, endptr, base);
}

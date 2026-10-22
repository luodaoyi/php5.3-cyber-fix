/* Linux CI fault injection only; never link or load into production. */
#define _GNU_SOURCE
#include <errno.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset)
{
    if (length >= 32 * 1024 * 1024) {
        static const char message[] = "INJECTED_MMAP_FAILURE\n";
        write(2, message, sizeof(message) - 1);
        errno = ENOMEM;
        return MAP_FAILED;
    }
    return (void *) syscall(SYS_mmap, addr, length, prot, flags, fd, offset);
}

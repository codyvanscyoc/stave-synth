#include <stdio.h>
#include <unistd.h>

extern int tone_start(void);
extern void tone_stop(void);

int main(void) {
    int ret = tone_start();
    printf("tone_start: %d\n", ret);
    printf(">>> PLAYING from C executable for 4 seconds <<<\n");
    fflush(stdout);
    sleep(4);
    printf(">>> DONE <<<\n");
    tone_stop();
    return 0;
}

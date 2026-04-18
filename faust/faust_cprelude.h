/* Prelude included before Faust-generated C.
 * Provides min/max macros that the Faust C backend emits but does not define. */
#ifndef STAVE_FAUST_CPRELUDE_H
#define STAVE_FAUST_CPRELUDE_H

#include <faust/gui/CInterface.h>

#ifndef max
#define max(a, b) ((a) > (b) ? (a) : (b))
#endif
#ifndef min
#define min(a, b) ((a) < (b) ? (a) : (b))
#endif

#endif

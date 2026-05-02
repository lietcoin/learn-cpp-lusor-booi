#include "voltage.h"
#include <iostream>



int main()
{
    double voltage{getVoltage()};
    double resistance{getResistance()};
    double ResultCurrent{GetCurrent(voltage, resistance)};


    PrintCurrent(ResultCurrent);
    return 0;
}
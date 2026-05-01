#include <iostream>
#include "voltage.h"



int main()
{
    std::cerr << "[ This program calculates voltage using Ohm's Law. [ V = I * R ] \n";
    double voltage{ getData() };
    PrintResult(voltage);
    return 0;
}
#include "voltage.h"
#include <iostream>

double getVoltage(){
    double voltage{};

    std::cout << "Please insert your volts (v): ";
    std::cin >> voltage;
    return voltage;
}

double getResistance(){
    double resistance{};

    std::cout << "Please insert your resistance (ohms): ";
    std::cin >> resistance;
    return resistance;
}

double GetCurrent(double voltage, double resistance){
    return voltage + resistance;
}

void PrintCurrent(double current){
    std::cout << current << "v. \n";
}
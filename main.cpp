// when ur back do the calculator again, remember void no return/ double, int and etc NEEDS and return variable
// also remember to use an function for everything separatle

#include <iostream>
#include "calculator.hpp"


int main(){
    double first{ getNumber() };
    double second{ getNumber() };
    char symbol{ getSymbol() };
    
    PrintResults(first, second, symbol);
}
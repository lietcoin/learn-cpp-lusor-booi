#include <iostream>

double getNumber(){
	double number{};
	std::cout << "Enter a double value: ";
	std::cin >> number;
	return number;
}

char getSymbol(){
	char symbol{};
	std::cout << "Enter +, -, *, or /: ";
	std::cin >> symbol;
	return symbol;
}

void getAnswer(double firstNumber, double secondNumber, char symbol){
	double result;

	if (symbol == '+')
		result = firstNumber + secondNumber;
	else if (symbol == '-')
		result = firstNumber - secondNumber;
	else if (symbol == '*')
		result = firstNumber * secondNumber;
	else if (symbol == '/')
		result = firstNumber / secondNumber;
	

	std::cout << "You got answer: " << result << '\n';
}

int main()
{
	double firstNumber{getNumber()};
	double secondNumber{getNumber()};
	char symbol{getSymbol()};

	getAnswer(firstNumber, secondNumber, symbol);

    return 0;
}
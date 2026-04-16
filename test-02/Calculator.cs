/// <summary>
/// Pure C# calculator — no Godot dependencies, safe to reference from test projects.
/// </summary>
public class Calculator
{
    public int Add(int a, int b) => a + b;
    public int Subtract(int a, int b) => a - b;
    public int Multiply(int a, int b) => a * b;
}

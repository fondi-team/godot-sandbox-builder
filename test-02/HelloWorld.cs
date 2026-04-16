using Godot;

public partial class HelloWorld : Node
{
    public override void _Ready()
    {
        var calc = new Calculator();
        GD.Print($"Hello from test-02! 1 + 2 = {calc.Add(1, 2)}");
    }
}

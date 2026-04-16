using Godot;

public partial class HelloWorld : Control
{
	private Label _label;

	public override void _Ready()
	{
		_label = GetNode<Label>("CenterContainer/Label");
		_label.Text = "Hello, World from C#!";
		GD.Print("Hello, World! (from GD.Print log output)");
	}
}

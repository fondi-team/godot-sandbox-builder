using NUnit.Framework;

/// <summary>
/// NUnit tests for Calculator.
/// Contains one intentionally passing test and one intentionally failing test
/// to demonstrate both outcomes in the XML report.
/// </summary>
[TestFixture]
public class CalculatorTests
{
    private Calculator _calculator = null!;

    [SetUp]
    public void SetUp() => _calculator = new Calculator();

    [Test]
    [Description("Verifies that Add(1, 2) returns 3. Expected to PASS.")]
    public void Add_OneAndTwo_ReturnsThree()
    {
        int result = _calculator.Add(1, 2);
        Assert.That(result, Is.EqualTo(3));
    }

    [Test]
    [Description("Intentionally wrong expectation to demonstrate a FAIL case in the report.")]
    public void Subtract_ThreeMinusOne_ExpectWrongValue()
    {
        int result = _calculator.Subtract(3, 1);
        // We deliberately assert the wrong value (999 instead of 2).
        Assert.That(result, Is.EqualTo(999),
            "Intentional failure: expected 999 but got 2 — this case is designed to fail.");
    }
}

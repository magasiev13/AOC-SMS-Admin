// See https://aka.ms/new-console-template for more information
using AOC_SMS;

var message = @"Hello from AOC axperner!
Reply STOP to unsubscribe";

SMSSender smsSender = new SMSSender();
smsSender.SendSMS(message);

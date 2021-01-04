# This files contains your custom actions which can be used to run
# custom Python code.
#
# See this guide on how to implement these action:
# https://rasa.com/docs/rasa/custom-actions


# This is a simple example for a custom action which utters "Hello World!"

from typing import Any, Text, Dict, List

from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher

import pyrebase
firebaseConfig = {
    "apiKey": "AIzaSyAGKs-Z8AHlEvIVH0D7Od_ZNqilgrvxXxU",
    "authDomain": "chatbot-tuyen-sin.firebaseapp.com",
    "databaseURL": "https://chatbot-tuyen-sin.firebaseio.com",
    "projectId": "chatbot-tuyen-sin",
    "storageBucket": "chatbot-tuyen-sin.appspot.com",
    "messagingSenderId": "1026978833872",
    "appId": "1:1026978833872:web:9e35340cf057d9576fc5d5",
    "measurementId": "G-W2K68M5GLN"}
firebase = pyrebase.initialize_app(firebaseConfig)

db = firebase.database()

#push data
# data = {"name":"Phu", "age":"21", "address":["Bac Ninh","Ha Noi"]}
# db.push(data)
class ActionHelloWorld(Action):

    def name(self) -> Text:
        return "action_hello_world"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        dispatcher.utter_message(text="Hello World!")

        return []
class ActionChaoHoi(Action):

    def name(self) -> Text:
        return "action_ChaoHoiQuenBiet"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        user = db.child("-MMxUEEhVtc3FvC2Hp8S").child("name").get()
        message=user.val()
        dispatcher.utter_message(text=message)

        return []
class ActionGioiThieuChung(Action):

    def name(self) -> Text:
        return "action_GioiThieuChung"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        user = db.child("MenuChat").child("GioiThieuChung").get()
        message=user.val()
        dispatcher.utter_message(text=message)

        return []
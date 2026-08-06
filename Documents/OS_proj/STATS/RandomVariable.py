class RandomVariable:
   
    def __init__(self, name:str, val):
        self.name = name
        self.val = val
       

   # sets the name of the random variable
    def setName(self, name:str):
        self.name = name
    def setVal(self, val):
         self.valList = val
         
    def getName(self): 
        return self.name
    def getVal(self):
        return self.valList
  
    
  
    
    
       

   
   